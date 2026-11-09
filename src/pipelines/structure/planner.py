"""
结构化规划：Markdown → pandoc AST → 单元 → 块（TextChunk + StructuredChunk 载荷）。

规划是确定性的：同一份源 Markdown + 同样的分块参数总能得到相同的块与 chunk_id，
因此导出、人工审校后的重渲染都可以从保存的源文重新规划，无需持久化 AST。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ...domain.document import document_ir
from ...domain.rules.chunk_planning import TextChunk
from ...infrastructure.document import pandoc


@dataclass
class StructuredPlan:
    ir: document_ir.DocumentIR
    chunks: list[TextChunk]

    @property
    def structured_chunks(self) -> list[document_ir.StructuredChunk]:
        return [chunk.structured for chunk in self.chunks]


class StructuredDocumentPlanner:
    def __init__(
        self,
        chunk_size: int = 3600,
        target_chunk_size: int | None = None,
        min_chunk_size: int | None = None,
        context_window: int = 1400,
    ):
        self.chunk_size = chunk_size
        self.target_chunk_size = min(target_chunk_size or int(chunk_size * 0.9), chunk_size)
        self.min_chunk_size = min_chunk_size or max(600, int(self.target_chunk_size * 0.4))
        self.context_window = context_window

    @classmethod
    def from_config(cls, config) -> StructuredDocumentPlanner:
        return cls(
            chunk_size=config.chunk_size,
            target_chunk_size=config.target_chunk_size,
            min_chunk_size=config.min_chunk_size,
            context_window=config.context_window,
        )

    def plan(self, text: str) -> StructuredPlan:
        ast = pandoc.markdown_to_ast(text)
        ir = document_ir.DocumentIR(ast=ast, units=document_ir.plan_units(ast, self.chunk_size))
        structured = document_ir.plan_chunks(ir, self.target_chunk_size, self.min_chunk_size)
        for item in structured:
            item.pandoc_api = ast["pandoc-api-version"]
        sources = pandoc.blocks_groups_to_markdown(ast, [item.blocks for item in structured])

        chunks: list[TextChunk] = []
        previous_tail = ""
        previous_section = ""
        for item, source in zip(structured, sources, strict=True):
            section_path = item.units[0].section_path if item.units else []
            section_title = section_path[-1] if section_path else "Document Root"
            parts = []
            if section_path:
                parts.append("当前章节: " + " > ".join(section_path))
            if previous_section and previous_section != section_title:
                parts.append(f"上一块章节: {previous_section}")
            if previous_tail:
                parts.append("上一块末尾:\n" + previous_tail)
            digest = hashlib.sha1(
                f"{item.index}|{' > '.join(section_path)}|{item.tagged_payload()}".encode()
            ).hexdigest()[:12]
            chunks.append(
                TextChunk(
                    index=item.index,
                    chunk_id=f"chunk-{item.index:04d}-{digest}",
                    text=source,
                    section_path=section_path,
                    section_title=section_title,
                    context_text="\n\n".join(parts),
                    structured=item,
                )
            )
            plain = " ".join(seg.plain for seg in item.segments if not seg.in_note)
            if plain:
                previous_tail = plain[-self.context_window :]
            previous_section = section_title
        return StructuredPlan(ir=ir, chunks=chunks)
