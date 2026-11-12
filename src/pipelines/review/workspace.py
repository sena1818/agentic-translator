"""
审校 agent 的工作区：一本书的段、术语表、待分诊问题，以及对候选译文重新质检的函数。

真实任务从全书索引（#13）载入段；评测时用同一个类，从段对直接构造索引。
重新质检默认是规则质检 + 残留英文 + 语义启发式（#20），只看纯文本；
非文字审计（#19）在评测里可以作为额外的检测器挂进来。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from ...core.validator import TranslationValidator
from ...domain.index.book_index import BookIndex, IndexedSegment
from ...domain.review.triage import CheckedIssue, FlaggedIssue
from ...domain.rules.semantic_signals import heuristic_findings

Recheck = Callable[[str, str, dict[str, str]], list[CheckedIssue]]


def segment_recheck(validator: TranslationValidator | None = None, semantic: bool = True) -> Recheck:
    """段级纯文本的重新质检：规则质检 + 残留英文 +（可选）语义启发式，与翻译时的段级质检同源。"""
    from ..translate.structured_translation import _HAS_WORDFREQ, _residual_english_words

    validator = validator or TranslationValidator()

    def recheck(source: str, translation: str, glossary: dict[str, str]) -> list[CheckedIssue]:
        issues = [
            CheckedIssue(issue.kind, issue.severity, issue.message)
            for issue in validator.validate(source, translation, glossary).issues
        ]
        residue = _residual_english_words(translation, glossary)
        if residue:
            severity = "medium" if _HAS_WORDFREQ else "low"
            issues.append(CheckedIssue("untranslated", severity, "残留英文单词: " + ", ".join(residue[:6])))
        if semantic:
            issues += [
                CheckedIssue("semantic", finding.severity, finding.message())
                for finding in heuristic_findings(source, translation)
            ]
        return issues

    return recheck


def recheck_for_config(config) -> Recheck:
    """按任务配置构造重新质检：术语大小写规则与翻译时一致；语义质检开着才查语义。"""
    from ...domain.rules.glossary_selection import respect_case

    get = getattr(config, "get", None) or (lambda _key, default=None: default)
    validator = TranslationValidator(glossary_respect_case=respect_case(config))
    return segment_recheck(validator, semantic=bool(get("quality.semantic_check.enabled", False)))


_OBJECTS = {
    "numbers": re.compile(r"(?<![0-9A-Za-z_.])\d+(?:[.,]\d+)*(?![0-9A-Za-z_])"),
    "links": re.compile(r"\]\(([^)\s]+)\)|https?://\S+"),
    "code": re.compile(r"`([^`\n]+)`"),
    "formulas": re.compile(r"\$[^$\n]+\$"),
    "references": re.compile(
        r"\b(?:Fig(?:ure)?s?\.?|Table|Eq(?:uation)?s?\.?|Section|Chapter)\s*[\dIVXLC]+(?:[-.]\d+)?"
        r"|[图表式节章]\s*[\d一二三四五六七八九十]+(?:[-.]\d+)?",
        re.I,
    ),
}


def nontext_objects(text: str) -> dict[str, list[str]]:
    """段内的数字、链接、行内代码、公式与图表编号引用（供审校 agent 对照原文与译文）。"""
    found = {}
    for name, pattern in _OBJECTS.items():
        values = [match.group(1) if match.lastindex else match.group(0) for match in pattern.finditer(text)]
        found[name] = values[:20]
    return found


class ReviewWorkspace:
    def __init__(
        self,
        index: BookIndex,
        glossary: dict[str, str],
        flagged: Sequence[FlaggedIssue],
        recheck: Recheck | None = None,
    ):
        self.index = index
        self.glossary = dict(glossary)
        self.flagged = list(flagged)
        self.recheck = recheck or segment_recheck()
        self._overrides: dict[int, str] = {}  # 本次分诊里已修的段：后续问题看到的是新译文

    @classmethod
    def from_pairs(
        cls,
        pairs: Sequence[tuple[str, str]],
        glossary: dict[str, str],
        flagged: Sequence[FlaggedIssue],
        recheck: Recheck | None = None,
    ) -> ReviewWorkspace:
        """评测用：段号从 1 开始、每段自成一块。"""
        segments = [
            IndexedSegment(i, i, source, translation, seg_id=i + 1, status="translated")
            for i, (source, translation) in enumerate(pairs)
        ]
        return cls(BookIndex(segments), glossary, flagged, recheck)

    def segment(self, seg_id: int) -> IndexedSegment | None:
        return self.index.by_seg_id(seg_id)

    def translation(self, seg_id: int) -> str:
        if seg_id in self._overrides:
            return self._overrides[seg_id]
        segment = self.segment(seg_id)
        return (segment.translation or "") if segment else ""

    def set_translation(self, seg_id: int, translation: str) -> None:
        self._overrides[seg_id] = translation

    def neighbors(self, seg_id: int, radius: int) -> list[IndexedSegment]:
        segment = self.segment(seg_id)
        if segment is None:
            return []
        positions = range(segment.position - radius, segment.position + radius + 1)
        return [found for p in positions if p != segment.position and (found := self.index.get(p)) is not None]

    def issues_on(self, seg_id: int) -> list[FlaggedIssue]:
        return [issue for issue in self.flagged if issue.seg_id == seg_id]

    def check(self, seg_id: int, translation: str) -> list[CheckedIssue]:
        segment = self.segment(seg_id)
        return self.recheck(segment.source if segment else "", translation, self.glossary)

    def glossary_lookup(self, term: str) -> dict[str, str]:
        needle = term.strip().lower()
        return {en: zh for en, zh in self.glossary.items() if needle and needle in en.lower()}

    def renderings(self, term: str) -> dict:
        known = [zh.split(" (", 1)[0].strip() for en, zh in self.glossary.items() if en.lower() == term.lower()]
        distribution = self.index.renderings(term, known=known)
        return {
            "term": distribution.term,
            "segments": distribution.segments,
            "untranslated_segments": distribution.untranslated_segments,
            "renderings": [
                {"text": r.text, "count": r.count, "example_positions": list(r.positions[:5])}
                for r in distribution.renderings
            ],
        }
