"""
把书内纠正传播落到块译文（Markdown）上。

预览按段给出（全书顺序号）；应用时要改的是块级 Markdown。两种写回方式：
- 该块里所有可替换的旧译名都落在选中的段里：直接在 Markdown 字符串上替换，其余字符一个不动；
- 否则（同一块里还有没选中、或原文不含该术语的段也用了旧译名）：解析成 AST，
  只改选中段的文字节点，再用 pandoc 写回。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ...domain.document import document_ir
from ...domain.memory.corrections import replace_unprotected, replaceable_spans
from ...infrastructure.document import pandoc

_INLINE_CHILDREN = {
    "Emph": lambda c: c,
    "Strong": lambda c: c,
    "Strikeout": lambda c: c,
    "Underline": lambda c: c,
    "Superscript": lambda c: c,
    "Subscript": lambda c: c,
    "SmallCaps": lambda c: c,
    "Quoted": lambda c: c[1],
    "Cite": lambda c: c[1],
    "Link": lambda c: c[1],
    "Image": lambda c: c[1],
    "Span": lambda c: c[1],
}


@dataclass(frozen=True)
class ChunkEdit:
    chunk_index: int
    translation: str  # 写回后的块译文 Markdown
    replacements: int
    method: str  # text | ast


def translatable_candidates(structured: document_ir.StructuredChunk) -> list[int]:
    """块内第 k 个含文字的候选在全部候选中的序号；全书顺序号 = 块首段顺序号 + k。"""
    return [
        index
        for index, inlines in enumerate(document_ir.iter_candidates(structured.blocks))
        if document_ir.is_translatable(inlines)
    ]


def _replace_in_inlines(inlines: list[dict], old: str, new: str, protected: Sequence[str]) -> int:
    """就地替换一个候选里的文字节点；不进入脚注（脚注正文是单独的候选）。"""
    count = 0
    for node in inlines:
        kind = node.get("t")
        if kind == "Str":
            node["c"], replaced = replace_unprotected(node["c"], old, new, protected)
            count += replaced
        elif kind in _INLINE_CHILDREN:
            count += _replace_in_inlines(_INLINE_CHILDREN[kind](node["c"]), old, new, protected)
    return count


def apply_to_chunk(
    structured: document_ir.StructuredChunk,
    translation: str,
    first_position: int,
    positions: Iterable[int],
    old: str,
    new: str,
    protected: Sequence[str] = (),
) -> ChunkEdit | None:
    """在选中的段（全书顺序号）里把旧译名换成新译名。块结构对不上或没有可替换处时返回 None。"""
    selected = {position - first_position for position in positions}
    blocks = pandoc.markdown_groups_to_blocks([translation])[0]
    targets = document_ir.translated_candidates(structured.blocks, blocks)
    if targets is None:
        return None
    ordinals = translatable_candidates(structured)
    chosen = [ordinals[k] for k in sorted(selected) if 0 <= k < len(ordinals)]
    replaced = sum(_replace_in_inlines(targets[index], old, new, protected) for index in chosen)
    if not replaced:
        return None
    if len(replaceable_spans(translation, old, protected)) == replaced:
        text, count = replace_unprotected(translation, old, new, protected)
        if count == replaced:
            return ChunkEdit(structured.index, text, replaced, "text")
    rendered = pandoc.blocks_to_markdown({"pandoc-api-version": structured.pandoc_api}, blocks)
    return ChunkEdit(structured.index, rendered, replaced, "ast")
