"""语义检索用例：在全书索引之上维护段级向量缓存，并做混合检索。

- 向量在翻译完成（导出）后生成并缓存；检索前再按文本哈希补齐，人工编辑过的段只重算这几段。
- 未配置向量模型、或模型不支持查询语言时，退回仅关键词排序，结果里带明确提示。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from pathlib import Path

from ...domain.index.book_index import IndexedSegment
from ...domain.index.semantic_search import MODE_LEXICAL, SemanticPage, query_language, search
from ...infrastructure.embeddings import Embedder, EmbedderSetup
from ...infrastructure.embeddings.base import NOT_CONFIGURED_HINT
from ...infrastructure.filesystem.task_files import safe_id
from ...infrastructure.persistence.vector_store import VectorStore
from .book_index import BookIndexService, segment_to_dict

logger = logging.getLogger(__name__)

SIDE_SOURCE = "source"
SIDE_TRANSLATION = "translation"


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _texts(segment: IndexedSegment, languages: frozenset[str]) -> dict[str, str]:
    """一段要算向量的文本：英文原文（模型支持 en 时）与中文译文（支持 zh 时）。"""
    texts = {}
    if "en" in languages and segment.source.strip():
        texts[SIDE_SOURCE] = segment.source
    if "zh" in languages and (segment.translation or "").strip():
        texts[SIDE_TRANSLATION] = segment.translation
    return texts


class SemanticSearchService:
    def __init__(self, book_index: BookIndexService, setup: EmbedderSetup | None = None):
        self.book_index = book_index
        self.setup = setup or EmbedderSetup(None, NOT_CONFIGURED_HINT)

    @property
    def embedder(self) -> Embedder | None:
        return self.setup.embedder

    def store(self, task_id: str) -> VectorStore:
        return VectorStore(Path(self.book_index.root) / "index" / f"{safe_id(task_id)}.vectors.sqlite")

    def ensure_vectors(self, task_id: str, segments: Sequence[IndexedSegment] | None = None) -> int:
        """按文本哈希补齐缺失或过期的向量，删除已不存在的；返回新算的条数。未配置嵌入器时什么也不做。"""
        embedder = self.embedder
        if embedder is None:
            return 0
        if segments is None:
            index = self.book_index.load(task_id)
            segments = index.segments if index is not None else []
        store = self.store(task_id)
        if store.model() != embedder.name:
            store.reset(embedder.name)
        existing = store.hashes()
        wanted: dict[tuple[int, str], tuple[str, str]] = {}
        for segment in segments:
            for side, text in _texts(segment, embedder.languages).items():
                wanted[(segment.position, side)] = (text, _hash(text))
        stale = [key for key, (_, digest) in wanted.items() if existing.get(key) != digest]
        removed = [key for key in existing if key not in wanted]
        if removed:
            store.remove(removed)
        if not stale:
            return 0
        vectors = embedder.embed_documents([wanted[key][0] for key in stale])
        store.upsert((key[0], key[1], wanted[key][1], vector) for key, vector in zip(stale, vectors, strict=True))
        return len(stale)

    def search(self, task_id: str, query: str, limit: int = 10) -> dict:
        index = self.book_index.load(task_id)
        if index is None:
            raise ValueError("该任务还没有全书索引（仅结构化翻译模式会建立索引）")
        page = self._search(task_id, index.segments, query, limit)
        return page_to_dict(page, self.embedder.name if self.embedder and page.mode != MODE_LEXICAL else None)

    def _search(self, task_id: str, segments: Sequence[IndexedSegment], query: str, limit: int) -> SemanticPage:
        embedder = self.embedder
        if embedder is None:
            return search(segments, query, limit=limit, hint=self.setup.hint)
        language = query_language(query)
        if language not in embedder.languages:
            hint = f"当前向量模型不支持{'中文' if language == 'zh' else '英文'}查询，本次为仅关键词检索。"
            return search(segments, query, limit=limit, hint=hint)
        try:
            self.ensure_vectors(task_id, segments)
            vectors = self.store(task_id).load()
            query_vector = embedder.embed_query(query)
        except Exception as exc:  # 向量服务暂时不可用时不影响检索本身
            logger.warning("向量检索失败，退回仅关键词: %s", exc)
            return search(segments, query, limit=limit, hint=f"向量模型调用失败（{exc}），本次为仅关键词检索。")
        return search(segments, query, limit=limit, query_vector=query_vector, vectors=vectors)

    def delete(self, task_id: str) -> None:
        self.store(task_id).delete()


def page_to_dict(page: SemanticPage, model: str | None = None) -> dict:
    return {
        "query": page.query,
        "mode": page.mode,
        "model": model,
        "hint": page.hint,
        "items": [
            {
                **segment_to_dict(hit.segment),
                "score": hit.score,
                "lexical_rank": hit.lexical_rank,
                "vector_rank": hit.vector_rank,
                "similarity": hit.similarity,
            }
            for hit in page.items
        ],
    }
