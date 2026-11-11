"""
从 Document IR 抽取全书索引的段。

源文规划（StructuredDocumentPlanner）给出每块的候选与段；块译文是 Markdown，
解析回块后按候选顺序一一对应，就能拿到每段的译文纯文本。块译文结构与原文对不上时，
该块各段只保留原文并标为 unaligned，不硬配。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace

from ...domain.document import document_ir, inline_codec
from ...domain.index import book_index
from ...domain.index.book_index import IndexedSegment
from ...infrastructure.document import pandoc


def chunk_segments(
    structured: document_ir.StructuredChunk,
    first_position: int,
    translated_blocks: list[dict] | None = None,
    status: str = book_index.STATUS_SOURCE_ONLY,
) -> list[IndexedSegment]:
    """一个块的全部含文字候选 → 索引段。translated_blocks 为 None 时只有原文。"""
    candidates = list(document_ir.candidates_with_note_flag(structured.blocks))
    verbatim_flags = [unit.verbatim for unit in structured.units for _ in document_ir.iter_candidates([unit.block])]
    section_paths = [
        tuple(unit.section_path) for unit in structured.units for _ in document_ir.iter_candidates([unit.block])
    ]
    seg_ids = {seg.candidate_index: seg.seg_id for seg in structured.segments}

    targets = None
    if translated_blocks is not None:
        targets = document_ir.translated_candidates(structured.blocks, translated_blocks)
        if targets is None:
            status = book_index.STATUS_UNALIGNED

    segments: list[IndexedSegment] = []
    for candidate_index, (inlines, in_note) in enumerate(candidates):
        if not document_ir.is_translatable(inlines):
            continue
        translation = inline_codec.plain_text(targets[candidate_index]) if targets is not None else None
        segments.append(
            IndexedSegment(
                position=first_position + len(segments),
                chunk_index=structured.index,
                source=inline_codec.plain_text(inlines),
                translation=translation,
                seg_id=seg_ids.get(candidate_index),
                section_path=section_paths[candidate_index],
                in_note=in_note,
                verbatim=verbatim_flags[candidate_index],
                status=status,
            )
        )
    return segments


Chunks = Sequence[document_ir.StructuredChunk]


def first_positions(chunks: Chunks) -> dict[int, int]:
    """每块第一段的全书顺序号（规划是确定的，同一源文总得到相同编号）。"""
    positions: dict[int, int] = {}
    next_position = 0
    for structured in chunks:
        positions[structured.index] = next_position
        next_position += sum(
            1 for inlines in document_ir.iter_candidates(structured.blocks) if document_ir.is_translatable(inlines)
        )
    return positions


def source_segments(chunks: Chunks) -> list[IndexedSegment]:
    """翻译前：只有原文的全书索引。"""
    positions = first_positions(chunks)
    return [segment for structured in chunks for segment in chunk_segments(structured, positions[structured.index])]


def translated_segments(chunks: Chunks, translations: Mapping[int, str | None]) -> list[IndexedSegment]:
    """给定若干块的译文 Markdown（None 表示该块翻译失败），返回这些块的索引段。"""
    positions = first_positions(chunks)
    by_index = {item.index: item for item in chunks}
    present = [(index, text) for index, text in translations.items() if index in by_index]
    parsed = pandoc.markdown_groups_to_blocks([text for _, text in present if text is not None])
    blocks_by_index = dict(zip([index for index, text in present if text is not None], parsed, strict=True))

    segments: list[IndexedSegment] = []
    for index, text in present:
        structured = by_index[index]
        if text is None:
            segments += [
                _with_status(seg, book_index.STATUS_FAILED) for seg in chunk_segments(structured, positions[index])
            ]
            continue
        segments += chunk_segments(structured, positions[index], blocks_by_index[index], book_index.STATUS_TRANSLATED)
    return segments


def record_translations(records: Iterable[dict]) -> dict[int, str | None]:
    """翻译记录 → {块号: 译文 Markdown 或 None（失败）}。"""
    return {
        int(record["chunk_index"]): (record.get("translation") if record.get("success") else None) for record in records
    }


def _with_status(segment: IndexedSegment, status: str) -> IndexedSegment:
    return replace(segment, status=status)
