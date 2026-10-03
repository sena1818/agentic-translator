"""
术语表外重复短语的译法一致率 —— RAG 门槛决策的量化依据。

问题：不在术语表里、但在文档中反复出现的英文短语，模型每次是否译成同一个中文？
若一致率高，说明单块翻译已能自洽，不需要文档内翻译记忆（RAG）；若低，则重复短语译法飘忽，
值得为其立项 sqlite-vec 翻译记忆。门槛取 0.90（见 issue #6 / ADR-0002）。

方法（见 docs/evals/methodology.md）：
1. 输入为按块对齐的 (原文, 译文) 段落对（评测直接取每个 chunk 的 result.original/translation）；
2. 在原文侧抽取跨段重复、且未被术语表覆盖的英文 n-gram 作为候选短语；
3. 对每个候选短语，取其所有出现段落的译文，求它们的最长公共中文子串作为"译法签名"：
   有足够长的公共中文子串 → 判为译法一致，否则不一致；
4. 一致率 = 一致短语数 / 候选短语数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_CJK_RE = re.compile(r"[一-鿿]")
# 句子边界：n-gram 不跨句拼接，避免 "baseline the buried engine" 这类跨句号的伪短语
_SENTENCE_SPLIT_RE = re.compile(r"[.!?;:\n]+")

# 只由停用词构成的短语没有指纹价值，剔除
_STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "to",
    "in",
    "on",
    "for",
    "and",
    "or",
    "but",
    "with",
    "as",
    "at",
    "by",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "it",
    "its",
    "this",
    "that",
    "these",
    "those",
    "from",
    "into",
    "than",
    "then",
    "so",
}


@dataclass(frozen=True)
class AlignedSegment:
    """一段按块对齐的原文与译文。"""

    source: str
    translation: str


@dataclass
class PhraseConsistency:
    """单个重复短语的一致性判定。"""

    phrase: str
    occurrences: int
    consistent: bool
    signature: str  # 译法签名（公共中文子串），不一致时为空


@dataclass
class ConsistencyReport:
    """一致率统计与 RAG 门槛决策。"""

    rate: float | None
    threshold: float
    total_phrases: int
    consistent_phrases: int
    decision: str  # "no-rag"（≥门槛，写 ADR 不做）| "build-rag"（<门槛，立项）
    phrases: list[PhraseConsistency] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "rate": self.rate,
            "threshold": self.threshold,
            "total_phrases": self.total_phrases,
            "consistent_phrases": self.consistent_phrases,
            "decision": self.decision,
            "phrases": [
                {
                    "phrase": p.phrase,
                    "occurrences": p.occurrences,
                    "consistent": p.consistent,
                    "signature": p.signature,
                }
                for p in self.phrases
            ],
        }


def _tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in _WORD_RE.finditer(text)]


def _glossary_terms(glossary: dict[str, str]) -> list[str]:
    return [term.lower() for term in glossary]


def _covered_by_glossary(phrase: str, glossary_terms: list[str]) -> bool:
    """短语与任一术语互为子串即视为被术语表覆盖，跳过。"""
    return any(phrase in term or term in phrase for term in glossary_terms)


def _longest_common_substring(a: str, b: str) -> str:
    """两串的最长公共子串（标准 DP）。"""
    if not a or not b:
        return ""
    prev = [0] * (len(b) + 1)
    best_len = 0
    best_end = 0
    for i in range(1, len(a) + 1):
        curr = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                curr[j] = prev[j - 1] + 1
                if curr[j] > best_len:
                    best_len = curr[j]
                    best_end = i
        prev = curr
    return a[best_end - best_len : best_end]


def _common_cjk_signature(translations: list[str], min_len: int) -> str:
    """求多条译文的最长公共子串，要求其中含中文且长度达标。"""
    if not translations:
        return ""
    signature = translations[0]
    for text in translations[1:]:
        signature = _longest_common_substring(signature, text)
        if len(signature) < min_len:
            return ""
    if len(signature) >= min_len and _CJK_RE.search(signature):
        return signature
    return ""


def _candidate_phrases(
    segments: list[AlignedSegment],
    glossary_terms: list[str],
    min_n: int,
    max_n: int,
    min_occurrences: int,
) -> dict[str, set[int]]:
    """抽取跨段重复、未被术语表覆盖、非纯停用词的英文 n-gram 及其出现段落集合。"""
    phrase_segments: dict[str, set[int]] = {}
    for seg_index, segment in enumerate(segments):
        seen_in_segment: set[str] = set()
        for sentence in _SENTENCE_SPLIT_RE.split(segment.source):
            tokens = _tokenize(sentence)
            for n in range(min_n, max_n + 1):
                for start in range(len(tokens) - n + 1):
                    gram_tokens = tokens[start : start + n]
                    # 首尾为停用词的 n-gram（如 "the desert"、"not the"）多是虚词残片，
                    # 对翻译记忆无价值且会虚高一致率，剔除
                    if gram_tokens[0] in _STOPWORDS or gram_tokens[-1] in _STOPWORDS:
                        continue
                    phrase = " ".join(gram_tokens)
                    if phrase in seen_in_segment:
                        continue
                    seen_in_segment.add(phrase)
                    if _covered_by_glossary(phrase, glossary_terms):
                        continue
                    phrase_segments.setdefault(phrase, set()).add(seg_index)

    repeated = {phrase: segs for phrase, segs in phrase_segments.items() if len(segs) >= min_occurrences}
    return _drop_nested(repeated)


def _drop_nested(phrase_segments: dict[str, set[int]]) -> dict[str, set[int]]:
    """去掉被更长短语完全包含、且出现段落相同的子短语，避免重复计数。"""
    kept: dict[str, set[int]] = {}
    phrases = sorted(phrase_segments, key=len, reverse=True)
    for phrase in phrases:
        segs = phrase_segments[phrase]
        contained = any(phrase != longer and phrase in longer and phrase_segments[longer] == segs for longer in kept)
        if not contained:
            kept[phrase] = segs
    return kept


def compute_consistency(
    segments: list[AlignedSegment],
    glossary: dict[str, str] | None = None,
    *,
    threshold: float = 0.90,
    min_n: int = 2,
    max_n: int = 4,
    min_occurrences: int = 2,
    min_signature_len: int = 2,
) -> ConsistencyReport:
    """计算术语表外重复短语的译法一致率并给出 RAG 门槛决策。

    无候选短语时标记样本不足，不输出质量结论。
    """
    glossary_terms = _glossary_terms(glossary or {})
    candidates = _candidate_phrases(segments, glossary_terms, min_n, max_n, min_occurrences)

    phrase_reports: list[PhraseConsistency] = []
    for phrase, seg_indices in sorted(candidates.items(), key=lambda item: (-len(item[1]), item[0])):
        translations = [segments[i].translation for i in sorted(seg_indices)]
        signature = _common_cjk_signature(translations, min_signature_len)
        phrase_reports.append(
            PhraseConsistency(
                phrase=phrase,
                occurrences=len(seg_indices),
                consistent=bool(signature),
                signature=signature,
            )
        )

    total = len(phrase_reports)
    consistent = sum(1 for p in phrase_reports if p.consistent)
    rate = round(consistent / total, 4) if total else None
    decision = "insufficient-data" if rate is None else ("no-rag" if rate >= threshold else "build-rag")

    return ConsistencyReport(
        rate=rate,
        threshold=threshold,
        total_phrases=total,
        consistent_phrases=consistent,
        decision=decision,
        phrases=phrase_reports,
    )
