"""
块级处理共享原语

缓存命中时由同一处构造结果对象与进度事件，保证续跑与实时翻译的产出格式一致。
"""
from __future__ import annotations

from ...core.chunk_planner import TextChunk
from ...domain.models.translation_models import TranslationResult


def build_cached_result(chunk: TextChunk, cache_entry) -> TranslationResult:
    """由缓存条目构造块翻译结果。"""
    return TranslationResult(
        chunk_index=chunk.index,
        original=chunk.text,
        translation=cache_entry.translation,
        success=True,
        retry_count=0,
        duration=0.0,
        chunk_id=chunk.chunk_id,
        quality_report=cache_entry.quality_report,
        repaired=cache_entry.repaired,
        cached=True,
    )


def cached_progress_event(chunk: TextChunk, cache_entry) -> dict:
    """缓存命中时上报的进度事件。"""
    return {
        "chunk_index": chunk.index,
        "chunk_id": chunk.chunk_id,
        "status": "completed",
        "translation": cache_entry.translation,
        "duration": 0.0,
        "quality_report": cache_entry.quality_report,
        "repaired": cache_entry.repaired,
        "cached": True,
    }
