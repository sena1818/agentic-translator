"""语义检索：关键词排序、跨语言命中、向量融合、向量缓存与未配置时的退回。

全部用自撰小语料和确定性的假嵌入器，不加载模型、不联网。
"""

from __future__ import annotations

import pytest

from src.application.use_cases.book_index import BookIndexService
from src.application.use_cases.semantic_search import SemanticSearchService
from src.domain.index.book_index import STATUS_TRANSLATED, IndexedSegment
from src.domain.index.semantic_search import MODE_HYBRID, MODE_LEXICAL, LexicalRanker, search, tokenize
from src.infrastructure.embeddings import EmbedderSetup, build_embedder
from src.infrastructure.embeddings.base import NOT_CONFIGURED_HINT

CORPUS = [
    ("Petroleum seeps up through the desert and feeds the war machine.", "石油从沙漠里渗出，喂养着战争机器。"),
    (
        "The archaeologist vanished while digging near the ruins of the old city.",
        "考古学家在旧城废墟附近挖掘时失踪了。",
    ),
    ("Dust storms carry the memory of buried things across the borders.", "沙尘暴把被掩埋之物的记忆带过边界。"),
    ("The cult kept a calendar of numbers that counted backwards to zero.", "这个教派保存着一部倒数至零的数字历法。"),
    ("Every tunnel under the mountain was sealed after the accident.", "事故之后，山下的每条隧道都被封死了。"),
]


def _segments() -> list[IndexedSegment]:
    return [
        IndexedSegment(position=i, chunk_index=0, seg_id=i + 1, source=src, translation=zh, status=STATUS_TRANSLATED)
        for i, (src, zh) in enumerate(CORPUS)
    ]


# 假嵌入器：按概念词表把文本映射到几个固定维度，近义词（含中英文）落在同一维。
_CONCEPTS = [
    ("oil", "petroleum", "crude", "石油", "原油"),
    ("vanish", "disappear", "missing", "失踪", "消失"),
    ("dust", "sand", "storm", "沙尘", "风暴"),
    ("calendar", "count", "number", "历法", "数字"),
    ("tunnel", "mine", "underground", "隧道", "地下"),
]


class FakeEmbedder:
    def __init__(self, languages=frozenset({"en", "zh"}), name="fake:v1"):
        self.name = name
        self.languages = languages
        self.documents_embedded = 0

    def _vector(self, text: str) -> list[float]:
        lowered = text.lower()
        return [float(sum(lowered.count(word) for word in words)) + 0.01 for words in _CONCEPTS]

    def embed_documents(self, texts):
        self.documents_embedded += len(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        return self._vector(text)


def _service(tmp_path, embedder=None) -> SemanticSearchService:
    books = BookIndexService(tmp_path)
    books.store("book").replace(_segments(), "fp")
    setup = EmbedderSetup(embedder, "") if embedder else EmbedderSetup(None, NOT_CONFIGURED_HINT)
    return SemanticSearchService(books, setup)


def test_tokenize_mixes_english_words_and_chinese_bigrams():
    assert tokenize("The cities were drilled") == ["city", "drill"]
    assert tokenize("战争机器") == ["战争", "争机", "机器"]


def test_paraphrase_with_partial_overlap_ranks_the_right_segment_first():
    ranked = LexicalRanker(_segments()).rank("someone digging at old ruins disappeared")
    assert ranked[0][0] == 1


def test_chinese_query_finds_wording_that_only_exists_in_the_english_source():
    # "war machine" 只在原文里是英文；中文查询经由该段译文命中同一段
    page = search(_segments(), "喂养战争机器的东西")
    assert page.items[0].segment.position == 0
    # 反过来：英文查询命中只在译文里才是中文的"倒数至零的历法"那一段
    page = search(_segments(), "a calendar counting down to zero")
    assert page.items[0].segment.position == 3


def test_unconfigured_embedder_falls_back_to_keywords_with_a_hint(tmp_path):
    result = _service(tmp_path).search("book", "petroleum in the desert")
    assert result["mode"] == MODE_LEXICAL and "未配置向量模型" in result["hint"]
    assert result["items"][0]["seg_id"] == 1 and result["items"][0]["translation"].startswith("石油")


def test_vectors_find_paraphrases_without_shared_words(tmp_path):
    service = _service(tmp_path, FakeEmbedder())
    assert search(_segments(), "crude").items == []  # 仅关键词排序：没有共同词
    hybrid = service.search("book", "where does the crude come from")
    assert hybrid["mode"] == MODE_HYBRID and hybrid["items"][0]["position"] == 0
    assert hybrid["items"][0]["vector_rank"] == 1 and hybrid["model"] == "fake:v1"
    assert service.search("book", "原油从哪里来")["items"][0]["position"] == 0


def test_vectors_are_cached_and_only_changed_segments_are_reembedded(tmp_path):
    embedder = FakeEmbedder()
    service = _service(tmp_path, embedder)
    assert service.ensure_vectors("book") == 10  # 5 段 × 原文、译文
    assert service.ensure_vectors("book") == 0
    edited = _segments()[2]
    service.book_index.store("book").update(
        [IndexedSegment(**{**edited.__dict__, "translation": "沙暴把埋藏之物的记忆带过国界。"})]
    )
    assert service.ensure_vectors("book") == 1
    assert embedder.documents_embedded == 11

    other = SemanticSearchService(service.book_index, EmbedderSetup(FakeEmbedder(name="fake:v2"), ""))
    assert other.ensure_vectors("book") == 10  # 换模型，旧向量作废


def test_english_only_model_uses_keywords_for_chinese_queries(tmp_path):
    service = _service(tmp_path, FakeEmbedder(languages=frozenset({"en"})))
    english = service.search("book", "petroleum")
    chinese = service.search("book", "石油")
    assert english["mode"] == MODE_HYBRID
    assert chinese["mode"] == MODE_LEXICAL and "不支持中文" in chinese["hint"]
    assert chinese["items"][0]["position"] == 0


def test_embedder_failure_degrades_to_keywords(tmp_path):
    class Broken(FakeEmbedder):
        def embed_query(self, text):
            raise RuntimeError("503")

    result = _service(tmp_path, Broken()).search("book", "petroleum")
    assert result["mode"] == MODE_LEXICAL and "调用失败" in result["hint"] and result["items"]


def test_missing_index_raises_and_delete_removes_vectors(tmp_path):
    service = _service(tmp_path, FakeEmbedder())
    with pytest.raises(ValueError, match="全书索引"):
        service.search("other", "oil")
    service.ensure_vectors("book")
    assert service.store("book").path.exists()
    service.delete("book")
    assert not service.store("book").path.exists()


def test_build_embedder_reports_why_it_is_unavailable(monkeypatch):
    class Config(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    assert build_embedder(Config()).hint == NOT_CONFIGURED_HINT
    monkeypatch.delenv("EMBED_KEY", raising=False)
    setup = build_embedder(
        Config(
            {
                "semantic_search.embedder": "openai",
                "semantic_search.model": "m",
                "semantic_search.api_key_env": "EMBED_KEY",
            }
        )
    )
    assert setup.embedder is None and "EMBED_KEY" in setup.hint
    missing = build_embedder(Config({"semantic_search.embedder": "local", "semantic_search.model": "no/such-model"}))
    assert missing.embedder is None and "不可用" in missing.hint
