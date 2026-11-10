"""
翻译质量校验器
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 中日韩表意文字、假名、谚文。中文标点不在此列，以便英文残留中混入的全角逗号句号不会打断计数。
_CJK = r"\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af"
_CJK_RE = re.compile(f"[{_CJK}]")
# 拉丁词：允许带重音字母、词内连字符和撇号。
_LATIN_WORD_RE = re.compile(r"[A-Za-z\u00C0-\u024F]+(?:['’\-][A-Za-z\u00C0-\u024F]+)*")
_LATIN_LETTER_RE = re.compile(r"[A-Za-z\u00C0-\u024F]")
# 英文片段的边界：CJK 字符、段落空行、表格竖线。
_RUN_BREAK_RE = re.compile(f"[{_CJK}]+|\\n[ \\t]*\\n|\\|")
_PARAGRAPH_BREAK_RE = re.compile(r"\n[ \t]*\n")

# 不属于自然语言正文、需在残留检测前剔除的片段。
_NON_PROSE_PATTERNS = [
    re.compile(r"^(```|~~~)[^\n]*\n[\s\S]*?^\1[^\n]*$", re.MULTILINE),  # 围栏代码块
    re.compile(r"```[\s\S]*?```"),  # 未独占行的围栏
    re.compile(r"`+[^`\n]*`+"),  # 行内代码
    re.compile(r"\$\$[\s\S]*?\$\$"),  # 块级公式
    re.compile(r"(?<![\\$])\$[^$\n]+\$"),  # 行内公式
    re.compile(r"\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)"),  # \[...\] 与 \(...\)
    re.compile(r"\]\([^)\s]*(?:\s+\"[^\"]*\")?\)"),  # Markdown 链接/图片目标
    re.compile(r"\[\^[^\]]*\]"),  # 脚注引用
    re.compile(r"<https?://[^>]*>|https?://\S+|www\.\S+"),  # URL
    re.compile(r"</?[A-Za-z][^>\n]*>"),  # HTML 标签
    re.compile(r"[\w/\\-]*(?:_[\w/\\-]*|\.[A-Za-z]\w*)+"),  # snake_case 标识符、文件名、module.attr
]
# 括注与书名号：`超虚构 (Hyperstition)`、`德勒兹（Gilles Deleuze）`、`《A Thousand Plateaus》`。
_ANNOTATION_RE = re.compile(r"\(([^()\n]*)\)|（([^（）\n]*)）|《([^《》\n]*)》|「([^「」\n]*)」")

# 片段中小写开头词的最低占比。正文句子大多是小写词，
# 人名、作品名、参考文献条目则以首字母大写词为主，据此避免误报。
_MIN_LOWERCASE_SHARE = 0.4


@dataclass
class QualityIssue:
    """单个质量问题"""

    kind: str
    severity: str
    message: str


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
        untranslated_latin_ratio: float = 0.7,
    ):
        # 连续英文词数阈值：词之间允许空白与标点，只被 CJK 字符或段落边界打断。
        self.untranslated_word_span = max(1, int(untranslated_word_span))
        self.max_glossary_checks = max_glossary_checks
        # 段落级阈值：英文词数达到 span 且拉丁字母占比不低于该值时视为未翻译；<=0 或 >1 关闭。
        self.untranslated_latin_ratio = float(untranslated_latin_ratio)

    def validate(
        self,
        original_text: str,
        translation: str,
        glossary: dict[str, str],
    ) -> QualityReport:
        issues: list[QualityIssue] = []

        if original_text.strip() and not translation.strip():
            issues.append(QualityIssue("empty", "high", "译文为空"))
        # 数字和公式属于需要保真的内容；代码、URL、公式不参与自然语言残留检查（在检查内部剔除）。
        issues.extend(self._check_untranslated_english(translation))
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
        repairable_kinds = {"untranslated", "terminology", "markdown", "empty", "fidelity", "truncated"}
        return any(issue.kind in repairable_kinds for issue in report.issues)

    def is_better(self, candidate: QualityReport, baseline: QualityReport) -> bool:
        """候选报告是否优于基线"""
        severity_score = {"high": 3, "medium": 2, "low": 1}
        baseline_score = sum(severity_score[issue.severity] for issue in baseline.issues)
        candidate_score = sum(severity_score[issue.severity] for issue in candidate.issues)
        return candidate_score <= baseline_score

    def _check_untranslated_english(self, translation: str) -> list[QualityIssue]:
        prose = self._strip_non_prose(translation)
        span = self.untranslated_word_span

        # 信号一：允许标点间隔的连续英文词（被 CJK 字符或段落边界打断）。
        for segment in _RUN_BREAK_RE.split(prose):
            words = _LATIN_WORD_RE.findall(segment)
            if len(words) >= span and self._looks_like_prose(words):
                return [self._untranslated_issue(segment)]

        # 信号二：段落里英文夹杂零星中文（例如逐词括注中文），按拉丁字母占比判定。
        if 0 < self.untranslated_latin_ratio <= 1:
            for paragraph in _PARAGRAPH_BREAK_RE.split(prose):
                words = _LATIN_WORD_RE.findall(paragraph)
                if len(words) < span or not self._looks_like_prose(words):
                    continue
                latin = len(_LATIN_LETTER_RE.findall(paragraph))
                cjk = len(_CJK_RE.findall(paragraph))
                if latin / (latin + cjk) >= self.untranslated_latin_ratio:
                    return [self._untranslated_issue(paragraph)]

        return []

    def _strip_non_prose(self, text: str) -> str:
        """剔除代码、公式、URL、HTML 及短括注，只留下需要翻译的自然语言正文。"""
        for pattern in _NON_PROSE_PATTERNS:
            text = pattern.sub(" ", text)

        def drop_short_annotation(match: re.Match) -> str:
            inner = next(group for group in match.groups() if group is not None)
            # 括注里若是整句英文（例如把原句附在括号里），仍保留给后续检测。
            if len(_LATIN_WORD_RE.findall(inner)) < self.untranslated_word_span:
                return " "
            return match.group(0)

        return _ANNOTATION_RE.sub(drop_short_annotation, text)

    @staticmethod
    def _looks_like_prose(words: list[str]) -> bool:
        lowercase = sum(1 for word in words if word[0].islower())
        return lowercase / len(words) >= _MIN_LOWERCASE_SHARE

    @staticmethod
    def _untranslated_issue(fragment: str) -> QualityIssue:
        first_word = _LATIN_WORD_RE.search(fragment)
        start = first_word.start() if first_word else 0
        snippet = " ".join(fragment[start:].split())[:80]
        return QualityIssue(
            kind="untranslated",
            severity="high",
            message=f"检测到疑似未翻译英文片段: {snippet}",
        )

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
