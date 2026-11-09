"""
翻译质量校验器
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class QualityIssue:
    """单个质量问题"""

    kind: str
    severity: str
    message: str
    segment: int | None = None  # 结构化模式下的全书段编号，供双栏对照定位


@dataclass
class QualityReport:
    """质量报告"""

    passed: bool
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def issue_count(self) -> int:
        return len(self.issues)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "issue_count": self.issue_count,
            "issues": [
                {
                    "kind": issue.kind,
                    "severity": issue.severity,
                    "message": issue.message,
                    **({"segment": issue.segment} if issue.segment is not None else {}),
                }
                for issue in self.issues
            ],
        }


class TranslationValidator:
    """基于规则的 chunk 质量校验器"""

    def __init__(
        self,
        untranslated_word_span: int = 12,
        max_glossary_checks: int = 25,
    ):
        self.untranslated_word_span = untranslated_word_span
        self.max_glossary_checks = max_glossary_checks

    def validate(
        self,
        original_text: str,
        translation: str,
        glossary: dict[str, str],
    ) -> QualityReport:
        issues: list[QualityIssue] = []

        if original_text.strip() and not translation.strip():
            issues.append(QualityIssue("empty", "high", "译文为空"))
        # 数字和公式属于需要保真的内容，代码和 URL 不参与自然语言残留检查。
        prose = re.sub(r"```[\s\S]*?```|`[^`]*`|https?://\S+", "", translation)
        issues.extend(self._check_untranslated_english(prose))
        numbers = set(re.findall(r"(?<![\w])\d+(?:[.,]\d+)*(?![\w])", original_text))
        missing = numbers - set(re.findall(r"(?<![\w])\d+(?:[.,]\d+)*(?![\w])", translation))
        if missing:
            issues.append(QualityIssue("fidelity", "medium", "缺少原文数字: " + ", ".join(sorted(missing))))
        formulas = re.findall(r"\$\$[\s\S]*?\$\$|(?<!\$)\$[^$\n]+\$", original_text)
        if any(formula not in translation for formula in formulas):
            issues.append(QualityIssue("fidelity", "high", "原文公式发生变化或缺失"))
        if len(original_text) > 300 and len(translation.strip()) < 20:
            issues.append(QualityIssue("truncated", "high", "译文疑似截断"))
        issues.extend(self._check_glossary(original_text, translation, glossary))
        issues.extend(self._check_markdown_structure(original_text, translation))
        issues.extend(self._check_repetition(translation))

        return QualityReport(passed=len(issues) == 0, issues=issues)

    def should_repair(self, report: QualityReport) -> bool:
        """是否值得触发修复"""
        repairable_kinds = {
            "untranslated", "terminology", "markdown", "empty", "fidelity", "truncated", "structure", "missing",
        }
        return any(issue.kind in repairable_kinds for issue in report.issues)

    def is_better(self, candidate: QualityReport, baseline: QualityReport) -> bool:
        """候选报告是否优于基线"""
        severity_score = {"high": 3, "medium": 2, "low": 1}
        baseline_score = sum(severity_score[issue.severity] for issue in baseline.issues)
        candidate_score = sum(severity_score[issue.severity] for issue in candidate.issues)
        return candidate_score <= baseline_score

    def _check_untranslated_english(self, translation: str) -> list[QualityIssue]:
        issues = []
        pattern = rf"[A-Za-z]+(?:\s+[A-Za-z]+){{{self.untranslated_word_span - 1},}}"
        match = re.search(pattern, translation)
        if match:
            issues.append(
                QualityIssue(
                    kind="untranslated",
                    severity="high",
                    message=f"检测到疑似未翻译英文片段: {match.group(0)[:80]}",
                )
            )
        return issues

    def _check_glossary(
        self,
        original_text: str,
        translation: str,
        glossary: dict[str, str],
    ) -> list[QualityIssue]:
        issues = []
        checked = 0

        for en_term, zh_term in sorted(glossary.items(), key=lambda item: len(item[0]), reverse=True):
            if checked >= self.max_glossary_checks:
                break

            if en_term.lower() not in original_text.lower():
                continue

            checked += 1
            preferred = zh_term.split(" (", 1)[0].strip()
            if preferred and preferred in translation:
                continue
            if zh_term in translation:
                continue

            issues.append(
                QualityIssue(
                    kind="terminology",
                    severity="medium",
                    message=f"术语 `{en_term}` 未体现为预期译名 `{preferred or zh_term}`",
                )
            )

        return issues

    def _check_markdown_structure(self, original_text: str, translation: str) -> list[QualityIssue]:
        issues = []

        counters = [
            ("标题", r"^#{1,6}\s+", "medium"),
            ("图片", r"!\[[^\]]*\]\([^)]+\)", "high"),
            ("链接", r"\[[^\]]+\]\([^)]+\)", "medium"),
            ("代码围栏", r"```", "high"),
        ]

        for label, pattern, severity in counters:
            original_count = len(re.findall(pattern, original_text, flags=re.MULTILINE))
            translated_count = len(re.findall(pattern, translation, flags=re.MULTILINE))
            if original_count != translated_count:
                issues.append(
                    QualityIssue(
                        kind="markdown",
                        severity=severity,
                        message=f"{label}数量不匹配: 原文 {original_count}, 译文 {translated_count}",
                    )
                )

        return issues

    def _check_repetition(self, translation: str) -> list[QualityIssue]:
        issues = []
        if re.search(r"(.{5,30}?)\1{4,}", translation):
            issues.append(
                QualityIssue(
                    kind="repetition",
                    severity="low",
                    message="检测到明显的重复短语",
                )
            )
        return issues
