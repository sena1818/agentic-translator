"""
翻译质量校验器
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..domain.rules.glossary_selection import term_occurs
from ..domain.rules.typed_repair import no_regression

_SCALES = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12}


def _format_number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _scaled_number_present(number: str, original: str, translation: str) -> bool:
    """ "160 million" 译作 "1.6亿"、"55 million" 译作 "5500万" 是正确的中文写法，不算数字丢失。"""
    match = re.search(rf"(?<![0-9A-Za-z_]){re.escape(number)}\s+(thousand|million|billion|trillion)\b", original, re.I)
    if not match:
        return False
    value = float(number.replace(",", "")) * _SCALES[match.group(1).lower()]
    compact = translation.replace(",", "").replace(" ", "")
    variants = {f"{_format_number(value / 1e4)}万", f"{_format_number(value / 1e8)}亿", _format_number(value)}
    if value.is_integer() and str(int(value)) in _chinese_numbers(translation):  # "一亿六千万"
        return True
    if match.group(1).lower() == "thousand":
        variants.add(f"{_format_number(value / 1e3)}千")
    return any(variant in compact for variant in variants if not variant.startswith("0"))


def _term_rendered(preferred: str, translation: str) -> bool:
    """译名是否在译文中体现。

    - 保留原文的拉丁译名（AxSys / Axsys、Ccru）大小写不敏感；
    - 形容词式译名的 "的" 不强求（"利莫里亚的" 在句中自然写成 "利莫里亚人"、"利莫里亚时间战争"）。
    """
    if not preferred:
        return False
    if preferred in translation:
        return True
    if preferred.isascii() and preferred.lower() in translation.lower():
        return True
    stem = preferred[:-1] if preferred.endswith("的") and len(preferred) > 2 else ""
    return bool(stem) and stem in translation


_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_NUMERAL_RE = re.compile(r"[零〇一二两三四五六七八九十百千万亿]+")


def _parse_chinese_numeral(text: str) -> int | None:
    """解析 "一亿六千万"、"七十"、"三" 这类中文数字；无法解析返回 None。"""
    total, section, number = 0, 0, 0
    for char in text:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            section += (number or 1) * _CN_UNITS[char]
            number = 0
        elif char == "万":
            section = (section + number) * 10_000
            total += section
            section, number = 0, 0
        elif char == "亿":
            total = (total + section + number) * 100_000_000
            section, number = 0, 0
        else:
            return None
    return total + section + number


def _chinese_numbers(translation: str) -> set[str]:
    """译文中以中文数字写出的数值（转为阿拉伯数字字符串），用于数字保真比对。"""
    values = set()
    for match in _CN_NUMERAL_RE.finditer(translation):
        value = _parse_chinese_numeral(match.group(0))
        if value is not None:
            values.add(str(value))
    return values


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
        glossary_respect_case: bool = False,
    ):
        self.untranslated_word_span = untranslated_word_span
        self.max_glossary_checks = max_glossary_checks
        self.glossary_respect_case = glossary_respect_case

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
        # 数字边界只看 ASCII 字母数字：中文紧贴数字（"2008年"、"47页"）是正常写法
        number_re = r"(?<![0-9A-Za-z_])\d+(?:[.,]\d+)*(?![0-9A-Za-z_])"
        numbers = set(re.findall(number_re, original_text))
        missing = numbers - set(re.findall(number_re, translation))
        written_in_chinese = _chinese_numbers(translation)
        missing = {
            number
            for number in missing
            if number.replace(",", "") not in written_in_chinese
            and not _scaled_number_present(number, original_text, translation)
        }
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
            "untranslated",
            "terminology",
            "markdown",
            "empty",
            "fidelity",
            "truncated",
            "structure",
            "missing",
        }
        return any(issue.kind in repairable_kinds for issue in report.issues)

    def is_better(self, candidate: QualityReport, baseline: QualityReport) -> bool:
        """候选报告是否优于基线：严重问题变少，且没有出现基线里没有的严重问题。

        旧做法按严重度加权求和、持平也接受，修好一类同时引入另一类时分数可能不变甚至更低。
        """
        return no_regression(baseline.issues, candidate.issues)

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

            if not term_occurs(en_term, original_text, self.glossary_respect_case):
                continue

            checked += 1
            preferred = zh_term.split(" (", 1)[0].strip()
            if _term_rendered(preferred, translation) or zh_term in translation:
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
