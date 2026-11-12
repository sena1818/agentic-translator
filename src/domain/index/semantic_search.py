"""
语义检索（按意思查找原话）：段级关键词排序 + 段级向量，用倒数排名融合（RRF）合并。

- 关键词排序（BM25）：每段的原文和译文一起参与。英文按词（小写、去常见虚词、粗略去词尾），
  中文按相邻两字（单字段落按单字）。与全书索引的"包含全部关键词"检索不同，它按重叠程度打分，
  改写过的句子只要有部分词重叠就能排上来。
- 向量：由上层传入查询向量和每段的向量（原文、译文各一条，取较高的相似度）。
  向量模型只支持英文时，上层只给英文查询算向量，中文查询退回关键词排序。
- 跨语言：一段同时带原文和译文，中文查询命中译文、英文查询命中原文都算命中这一段，
  所以"只在英文原文里出现的说法"用中文也能找到（经由该段译文）。

本模块不读写文件、不调用模型；向量由上层的嵌入器算好、缓存后传入。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .book_index import IndexedSegment

MODE_HYBRID = "hybrid"
MODE_LEXICAL = "lexical"

_EN_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
_CJK_RUN = re.compile(r"[㐀-鿿]+")
_CJK = re.compile(r"[㐀-鿿]")
_STOPWORDS = frozenset(
    """a an the of to in on for and or but with as at by is are was were be been being it its this that these those
    from into than then so not no do does did has have had he she they them his her their our we you your i me my
    which who whom what when where why how all any some such there here can could would should may might will shall
    upon also only just very more most other one""".split()
)
# 中文高频虚字组成的二字片段几乎不带内容信息
_ZH_STOP_BIGRAMS = frozenset({"的是", "是一", "一个", "了一", "这个", "那个", "我们", "他们", "它们", "之中", "之间"})
# RRF 常数：常用取值，让排名靠前的差异影响更大但不至于一项独大
RRF_K = 60


def _stem(word: str) -> str:
    """粗略去词尾：cities/city、drilled/drill、drilling/drill 归到同一形式；只为提高重叠，不求语言学正确。"""
    for suffix, keep in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + keep
    return word


def tokenize(text: str) -> list[str]:
    """英文词（小写、去虚词、去词尾）+ 中文二字片段。"""
    tokens: list[str] = []
    for match in _EN_WORD.finditer(text):
        word = match.group(0).lower().strip("'’-")
        if len(word) < 2 or word in _STOPWORDS:
            continue
        tokens.append(_stem(word))
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            tokens.append(run)
            continue
        tokens += [run[i : i + 2] for i in range(len(run) - 1) if run[i : i + 2] not in _ZH_STOP_BIGRAMS]
    return tokens


def query_language(query: str) -> str:
    """含汉字算中文查询，否则算英文；决定能否用只支持英文的向量模型。"""
    return "zh" if _CJK.search(query) else "en"


class LexicalRanker:
    """段级 BM25：文档 = 一段的原文 + 译文。"""

    def __init__(self, segments: Sequence[IndexedSegment], k1: float = 1.2, b: float = 0.75):
        self.segments = list(segments)
        self.k1, self.b = k1, b
        self.docs = [Counter(tokenize(f"{seg.source}\n{seg.translation or ''}")) for seg in self.segments]
        self.lengths = [sum(doc.values()) for doc in self.docs]
        self.avg_length = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        df: Counter[str] = Counter()
        for doc in self.docs:
            df.update(doc.keys())
        total = len(self.docs)
        self.idf = {term: math.log(1 + (total - n + 0.5) / (n + 0.5)) for term, n in df.items()}

    def rank(self, query: str) -> list[tuple[int, float]]:
        """返回 (段在列表中的下标, 分数)，按分数降序，只含分数 > 0 的段。"""
        terms = Counter(tokenize(query))
        if not terms or not self.docs:
            return []
        scored = []
        for i, doc in enumerate(self.docs):
            score = 0.0
            norm = self.k1 * (1 - self.b + self.b * self.lengths[i] / (self.avg_length or 1))
            for term, weight in terms.items():
                tf = doc.get(term)
                if tf:
                    score += weight * self.idf[term] * tf * (self.k1 + 1) / (tf + norm)
            if score > 0:
                scored.append((i, score))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def vector_rank(
    segments: Sequence[IndexedSegment],
    query_vector: Sequence[float],
    vectors: Mapping[int, Sequence[Sequence[float]]],
) -> list[tuple[int, float]]:
    """按向量相似度排序：每段取它各条向量（原文、译文）与查询的最高余弦。vectors 以段的 position 为键。"""
    scored = []
    for i, seg in enumerate(segments):
        candidates = vectors.get(seg.position)
        if candidates:
            scored.append((i, max(cosine(query_vector, vector) for vector in candidates)))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored


@dataclass(frozen=True)
class SemanticHit:
    segment: IndexedSegment
    score: float  # 融合分（RRF）；仅关键词模式下为 BM25 分
    lexical_rank: int | None  # 1 起；None 表示关键词排序没有命中
    vector_rank: int | None  # 1 起；None 表示没有用向量或该段无向量
    similarity: float | None  # 最高余弦相似度


@dataclass(frozen=True)
class SemanticPage:
    query: str
    mode: str  # hybrid | lexical
    hint: str  # 退回仅关键词时说明原因；正常为空
    items: list[SemanticHit]


def search(
    segments: Sequence[IndexedSegment],
    query: str,
    *,
    limit: int = 10,
    query_vector: Sequence[float] | None = None,
    vectors: Mapping[int, Sequence[Sequence[float]]] | None = None,
    hint: str = "",
    lexical: LexicalRanker | None = None,
    depth: int = 100,
) -> SemanticPage:
    """混合检索。没有查询向量或段向量时退回仅关键词排序，并在 hint 里说明。

    融合只取两路各自前 depth 名，避免长尾噪声累积分数。
    """
    query = query.strip()
    usable = [seg for seg in segments if seg.source.strip() or (seg.translation or "").strip()]
    if not query or not usable:
        return SemanticPage(query, MODE_LEXICAL, hint, [])
    lexical = lexical or LexicalRanker(usable)
    lex = lexical.rank(query)[:depth]
    lex_rank = {i: r for r, (i, _) in enumerate(lex, start=1)}

    if query_vector is None or not vectors:
        items = [SemanticHit(lexical.segments[i], round(score, 4), lex_rank[i], None, None) for i, score in lex]
        return SemanticPage(query, MODE_LEXICAL, hint, items[:limit])

    vec = vector_rank(lexical.segments, query_vector, vectors)[:depth]
    vec_rank = {i: r for r, (i, _) in enumerate(vec, start=1)}
    similarity = dict(vec)
    fused: dict[int, float] = {}
    for ranks in (lex_rank, vec_rank):
        for i, rank in ranks.items():
            fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank)
    order = sorted(fused, key=lambda i: (-fused[i], i))[:limit]
    items = [
        SemanticHit(
            lexical.segments[i],
            round(fused[i], 6),
            lex_rank.get(i),
            vec_rank.get(i),
            round(similarity[i], 4) if i in similarity else None,
        )
        for i in order
    ]
    return SemanticPage(query, MODE_HYBRID, hint, items)
