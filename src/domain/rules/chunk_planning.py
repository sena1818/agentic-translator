"""
结构化分块规则

从旧的 `src/core/chunk_planner.py` 迁移而来。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass
class TextBlock:
    """Markdown 结构块"""

    text: str
    section_path: list[str]
    is_heading: bool = False


@dataclass
class TextChunk:
    """结构化文本块"""

    index: int
    chunk_id: str
    text: str
    section_path: list[str]
    section_title: str
    context_text: str
    # 结构化模式（Document IR）下的段载荷；为 None 时走传统 Markdown 文本翻译
    structured: object | None = None


class ChunkPlanner:
    """章节感知的文本分块器"""

    def __init__(
        self,
        chunk_size: int = 3600,
        context_window: int = 1400,
        target_chunk_size: int | None = None,
        min_chunk_size: int | None = None,
    ):
        self.chunk_size = chunk_size
        self.context_window = context_window
        self.target_chunk_size = min(target_chunk_size or int(chunk_size * 0.9), chunk_size)
        self.min_chunk_size = min_chunk_size or max(600, int(self.target_chunk_size * 0.4))

    def plan(self, text: str) -> list[TextChunk]:
        """将完整文档切成结构化 chunk"""
        blocks = self._extract_blocks(text)
        raw_chunks = self._merge_short_chunks(self._pack_blocks(blocks))

        chunks: list[TextChunk] = []
        previous_text = ""
        previous_section_title = ""

        for index, raw in enumerate(raw_chunks):
            section_title = raw["section_path"][-1] if raw["section_path"] else "Document Root"
            context_text = self._build_context(
                previous_text=previous_text,
                section_path=raw["section_path"],
                previous_section_title=previous_section_title,
            )
            chunk_id = self._build_chunk_id(index, raw["section_path"], raw["text"])

            chunks.append(
                TextChunk(
                    index=index,
                    chunk_id=chunk_id,
                    text=raw["text"],
                    section_path=raw["section_path"],
                    section_title=section_title,
                    context_text=context_text,
                )
            )

            previous_text = raw["text"]
            previous_section_title = section_title

        return chunks

    def _extract_blocks(self, text: str) -> list[TextBlock]:
        lines = text.splitlines()
        blocks: list[TextBlock] = []
        heading_stack: list[str] = []
        buffer: list[str] = []
        in_code_fence = False

        def flush_buffer():
            if buffer:
                block_text = "\n".join(buffer).strip("\n")
                if block_text.strip():
                    blocks.append(TextBlock(text=block_text, section_path=heading_stack.copy()))
                buffer.clear()

        for line in lines:
            stripped = line.strip()

            if re.match(r"^```", stripped):
                if not in_code_fence:
                    flush_buffer()
                    in_code_fence = True
                    buffer.append(line)
                else:
                    buffer.append(line)
                    flush_buffer()
                    in_code_fence = False
                continue

            if in_code_fence:
                buffer.append(line)
                continue

            heading_match = re.match(r"^(#{1,6})\s+(.*)$", line)
            if heading_match:
                flush_buffer()
                level = len(heading_match.group(1))
                title = heading_match.group(2).strip()
                heading_stack = heading_stack[: level - 1]
                heading_stack.append(title)
                blocks.append(
                    TextBlock(
                        text=line.strip(),
                        section_path=heading_stack.copy(),
                        is_heading=True,
                    )
                )
                continue

            if stripped == "":
                flush_buffer()
                continue

            buffer.append(line)

        flush_buffer()
        return blocks

    def _pack_blocks(self, blocks: list[TextBlock]) -> list[dict]:
        raw_chunks: list[dict] = []
        current_lines: list[str] = []
        current_section_path: list[str] | None = None

        def flush_chunk():
            nonlocal current_lines, current_section_path
            if current_lines:
                text = "\n\n".join(current_lines).strip()
                if text:
                    raw_chunks.append(
                        {
                            "text": text,
                            "section_path": current_section_path.copy() if current_section_path else [],
                        }
                    )
                current_lines = []
                current_section_path = None

        for block in blocks:
            block_section = block.section_path.copy()
            block_text = block.text.strip()

            if not block_text:
                continue

            candidate_lines = current_lines + [block_text]
            candidate_text = "\n\n".join(candidate_lines)
            section_changed = current_section_path is not None and block_section != current_section_path
            would_overflow = len(candidate_text) > self.chunk_size and bool(current_lines)

            if section_changed or would_overflow or (block.is_heading and current_lines):
                flush_chunk()

            if current_section_path is None:
                current_section_path = block_section

            if len(block_text) > self.chunk_size:
                flush_chunk()
                for piece in self._split_oversized_block(block_text):
                    raw_chunks.append({"text": piece, "section_path": block_section})
                continue

            current_lines.append(block_text)

        flush_chunk()
        return raw_chunks

    def _merge_short_chunks(self, raw_chunks: list[dict]) -> list[dict]:
        if not raw_chunks:
            return raw_chunks

        merged: list[dict] = []
        for chunk in raw_chunks:
            if not merged:
                merged.append(chunk)
                continue

            previous = merged[-1]
            same_section = previous["section_path"] == chunk["section_path"]
            combined = f"{previous['text']}\n\n{chunk['text']}".strip()
            previous_is_short = len(previous["text"]) < self.min_chunk_size

            if same_section and previous_is_short and len(combined) <= self.chunk_size:
                previous["text"] = combined
                continue

            merged.append(chunk)

        if len(merged) >= 2:
            last = merged[-1]
            before_last = merged[-2]
            if len(last["text"]) < self.min_chunk_size and last["section_path"] == before_last["section_path"]:
                candidate = f"{before_last['text']}\n\n{last['text']}".strip()
                if len(candidate) <= self.chunk_size:
                    before_last["text"] = candidate
                    merged.pop()

        return merged

    def _split_oversized_block(self, text: str) -> list[str]:
        separators = ["\n\n", "\n", "。", "！", "？", ". ", "! ", "? ", " "]
        pieces = [text]

        for separator in separators:
            next_pieces: list[str] = []
            changed = False
            for piece in pieces:
                if len(piece) <= self.chunk_size:
                    next_pieces.append(piece)
                    continue

                parts = piece.split(separator)
                if len(parts) == 1:
                    next_pieces.append(piece)
                    continue

                changed = True
                current = ""
                for part in parts:
                    candidate = f"{current}{separator if current else ''}{part}".strip()
                    if current and len(candidate) > self.chunk_size:
                        next_pieces.append(current.strip())
                        current = part
                    else:
                        current = candidate
                if current.strip():
                    next_pieces.append(current.strip())

            pieces = next_pieces
            if changed and all(len(piece) <= self.chunk_size for piece in pieces):
                return pieces

        final_pieces: list[str] = []
        for piece in pieces:
            if len(piece) <= self.chunk_size:
                final_pieces.append(piece)
                continue
            for start in range(0, len(piece), self.chunk_size):
                final_pieces.append(piece[start:start + self.chunk_size])

        return final_pieces

    def _build_context(
        self,
        previous_text: str,
        section_path: list[str],
        previous_section_title: str,
    ) -> str:
        parts: list[str] = []

        if section_path:
            parts.append("当前章节: " + " > ".join(section_path))

        if previous_section_title and (not section_path or previous_section_title != section_path[-1]):
            parts.append(f"上一块章节: {previous_section_title}")

        if previous_text:
            parts.append("上一块末尾:\n" + previous_text[-self.context_window :])

        return "\n\n".join(parts).strip()

    def _build_chunk_id(self, index: int, section_path: list[str], text: str) -> str:
        payload = f"{index}|{' > '.join(section_path)}|{text}".encode()
        digest = hashlib.sha1(payload).hexdigest()[:12]
        return f"chunk-{index:04d}-{digest}"
