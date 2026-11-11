"""
术语表外重复短语的译法一致率 —— RAG 门槛决策的启发式参考。

问题：不在术语表里、但在文档中反复出现的英文短语，模型每次是否译成同一个中文？

方法（见 docs/evals/methodology.md）：
1. 输入为 (原文, 译文) 对（评测取每个 chunk 的 result.original/translation）；
2. 先把每对拆成段落、再拆成句子，只有两侧段数、句数都相同时才按顺序一一对齐；
   对不齐的单元不参与统计，只计数（宁可少算，不拿无关文字凑一致）；
3. 在原文句子里抽取跨句重复、且未被术语表覆盖的英文 n-gram 作为候选短语；
4. 对每个候选短语，取它每次出现所在句子的对应译句，求公共中文子串作为"译法签名"：
   有足够长的公共中文子串 → 一致，否则不一致；
5. 一致率 = 一致短语数 / 候选短语数。

旧做法在整块译文上求公共子串：两块里碰巧都有一句"这是一条共同注释。"，
就会让毫不相干的短语被判为一致。按句对齐后，签名只能来自短语所在的那一句。
这仍是启发式指标：同一句里另一个重复短语的译法也可能成为签名，结论不能替代人工核对。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_CJK_RE = re.compile(r"[一-鿿]")
# 句子边界：n-gram 不跨句拼接，避免 "baseline the buried engine" 这类跨句号的伪短语
_SENTENCE_SPLIT_RE = re.compile(r"[.!?;:\n]+")
# 对齐用的句末切分：英文句号/问号/叹号后接空白；中文句号/问号/叹号
_EN_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=[\"'“‘(\[]?[A-Z0-9])")
_ZH_SENTENCE_END_RE = re.compile(r"(?<=[。！？])(?![”’」』）)])")
_PARAGRAPH_RE = re.compile(r"\n\s*\n")

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
    aligned_units: int = 0  # 成功对齐的句对数
    unaligned_units: int = 0  # 段数或句数对不上、未参与统计的段落数

    def as_dict(self) -> dict:
        return {
            "rate": self.rate,
            "threshold": self.threshold,
            "total_phrases": self.total_phrases,
            "consistent_phrases": self.consistent_phrases,
            "decision": self.decision,
            "aligned_units": self.aligned_units,
            "unaligned_units": self.unaligned_units,
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


def _split_sentences(text: str, pattern: re.Pattern) -> list[str]:
    return [part.strip() for part in pattern.split(text.strip()) if part.strip()]


def align_sentences(segments: list[AlignedSegment]) -> tuple[list[AlignedSegment], int]:
    """把块级 (原文, 译文) 拆成句级对；返回 (对齐的句对, 未能对齐的段落数)。

    先按空行拆段，段数不同则整块算一个未对齐段落；段内再按句末标点拆句，句数不同则该段未对齐。
    中文常把两句并成一句、或把长句拆开，句数不等时宁可放弃，也不硬配。
    """
    aligned: list[AlignedSegment] = []
    unaligned = 0
    for segment in segments:
        sources = [p for p in _PARAGRAPH_RE.split(segment.source.strip()) if p.strip()]
        targets = [p for p in _PARAGRAPH_RE.split(segment.translation.strip()) if p.strip()]
        if len(sources) != len(targets):
            unaligned += max(len(sources), 1)
            continue
        for source, target in zip(sources, targets, strict=True):
            source_sentences = _split_sentences(source, _EN_SENTENCE_END_RE)
            target_sentences = _split_sentences(target, _ZH_SENTENCE_END_RE)
            if len(source_sentences) != len(target_sentences):
                unaligned += 1
                continue
            aligned += [AlignedSegment(s, t) for s, t in zip(source_sentences, target_sentences, strict=True)]
    return aligned, unaligned


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

    输入可以是块级对，内部先按句对齐（见 align_sentences）。
    无候选短语时标记样本不足，不输出质量结论。
    """
    segments, unaligned = align_sentences(segments)
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
        aligned_units=len(segments),
        unaligned_units=unaligned,
    )
