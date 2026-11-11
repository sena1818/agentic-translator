"""全书索引用例：建索引、随翻译进度与人工编辑增量更新、对外查询。

每本书（任务）一个索引文件 data/index/<task_id>.sqlite。只在 Document IR 模式下可用：
段的位置与对照阅读器一致，依赖结构化规划。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from ...domain.document import document_ir
from ...domain.index.book_index import BookIndex, IndexedSegment, OccurrencePage, RenderingDistribution, SearchPage
from ...infrastructure.filesystem.task_files import safe_id
from ...infrastructure.persistence.book_index_store import BookIndexStore
from ...pipelines.structure import indexing


def plan_fingerprint(chunks: Sequence[document_ir.StructuredChunk]) -> str:
    """源文与分块参数共同决定的指纹：任一变化都会让段位置失效。"""
    digest = hashlib.sha1()
    for chunk in chunks:
        digest.update(f"{chunk.index}:{document_ir.chunk_fingerprint(chunk)}:{chunk.candidate_count};".encode())
    return digest.hexdigest()[:16]


class BookIndexService:
    def __init__(self, root: str | Path = "data"):
        self.root = Path(root)

    def store(self, task_id: str) -> BookIndexStore:
        return BookIndexStore(self.root / "index" / f"{safe_id(task_id)}.sqlite")

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #

    def build_source(self, task_id: str, chunks: Sequence[document_ir.StructuredChunk]) -> bool:
        """翻译前建只含原文的索引；同一源文已有索引（续跑、重试）时保留已有译文。返回是否重建。"""
        store = self.store(task_id)
        fingerprint = plan_fingerprint(chunks)
        if store.fingerprint() == fingerprint:
            return False
        store.replace(indexing.source_segments(chunks), fingerprint)
        return True

    def update_chunks(
        self,
        task_id: str,
        chunks: Sequence[document_ir.StructuredChunk],
        translations: Mapping[int, str | None],
    ) -> int:
        """写入若干块的译文（None 表示失败）。索引不存在或源文已变时先重建。"""
        if not translations:
            return 0
        self.build_source(task_id, chunks)
        return self.store(task_id).update(indexing.translated_segments(chunks, translations))

    def update_records(
        self, task_id: str, chunks: Sequence[document_ir.StructuredChunk], records: Iterable[dict]
    ) -> int:
        return self.update_chunks(task_id, chunks, indexing.record_translations(records))

    def delete(self, task_id: str) -> None:
        self.store(task_id).delete()

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def load(self, task_id: str) -> BookIndex | None:
        store = self.store(task_id)
        return BookIndex(store.load()) if store.exists() else None

    def search(self, task_id: str, query: str, field: str = "any", offset: int = 0, limit: int = 20) -> dict:
        index = self._require(task_id)
        return {**search_to_dict(index.search(query, field=field, offset=offset, limit=limit)), "stats": index.stats}

    def occurrences(self, task_id: str, term: str, **options) -> dict:
        index = self._require(task_id)
        return {**occurrences_to_dict(index.occurrences(term, **options)), "stats": index.stats}

    def renderings(self, task_id: str, term: str, known: Sequence[str] = (), **options) -> dict:
        index = self._require(task_id)
        return {**renderings_to_dict(index.renderings(term, known=known, **options)), "stats": index.stats}

    def _require(self, task_id: str) -> BookIndex:
        index = self.load(task_id)
        if index is None:
            raise ValueError("该任务还没有全书索引（仅结构化翻译模式会建立索引）")
        return index


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #


def segment_to_dict(seg: IndexedSegment) -> dict:
    return {
        "position": seg.position,
        "seg_id": seg.seg_id,
        "chunk_index": seg.chunk_index,
        "section_path": list(seg.section_path),
        "in_note": seg.in_note,
        "verbatim": seg.verbatim,
        "status": seg.status,
        "source": seg.source,
        "translation": seg.translation,
    }


def occurrences_to_dict(page: OccurrencePage) -> dict:
    return {
        "term": page.term,
        "total_matches": page.total_matches,
        "total_segments": page.total_segments,
        "offset": page.offset,
        "limit": page.limit,
        "forms": page.forms,
        "items": [
            {
                **segment_to_dict(item.segment),
                "matches": [{"start": start, "end": end, "text": text} for start, end, text in item.matches],
                "before": item.before,
                "after": item.after,
            }
            for item in page.items
        ],
    }


def search_to_dict(page: SearchPage) -> dict:
    return {
        "query": page.query,
        "total": page.total,
        "offset": page.offset,
        "limit": page.limit,
        "items": [{**segment_to_dict(hit.segment), "field": hit.field} for hit in page.items],
    }


def renderings_to_dict(distribution: RenderingDistribution) -> dict:
    return {
        "term": distribution.term,
        "segments": distribution.segments,
        "untranslated_segments": distribution.untranslated_segments,
        "renderings": [
            {"text": item.text, "count": item.count, "positions": list(item.positions)}
            for item in distribution.renderings
        ],
    }
