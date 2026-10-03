"""
翻译领域模型
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass
class DocumentProfile:
    """文档分析 agent 产出的全局上下文"""

    summary: str = ""
    style_notes: list[str] = field(default_factory=list)
    terminology_hints: list[str] = field(default_factory=list)
    section_overview: list[str] = field(default_factory=list)

    @classmethod
    def empty(cls) -> DocumentProfile:
        return cls()

    @property
    def fingerprint(self) -> str:
        payload = "|".join(
            [
                self.summary,
                "\n".join(self.style_notes),
                "\n".join(self.terminology_hints),
                "\n".join(self.section_overview),
            ]
        ).encode("utf-8")
        return hashlib.sha1(payload).hexdigest()[:12]

    def to_prompt_text(self) -> str:
        if not any([self.summary, self.style_notes, self.terminology_hints, self.section_overview]):
            return "无额外文档级分析。"

        parts = []
        if self.summary:
            parts.append(f"文档摘要: {self.summary}")
        if self.style_notes:
            parts.append("风格约束:\n" + "\n".join(f"- {item}" for item in self.style_notes))
        if self.terminology_hints:
            parts.append("术语提示:\n" + "\n".join(f"- {item}" for item in self.terminology_hints))
        if self.section_overview:
            parts.append("章节概览:\n" + "\n".join(f"- {item}" for item in self.section_overview))
        return "\n\n".join(parts)


@dataclass
class TranslationResult:
    """翻译结果"""

    chunk_index: int
    original: str
    translation: str
    success: bool
    retry_count: int = 0
    duration: float = 0.0
    chunk_id: str = ""
    quality_report: dict | None = None
    repaired: bool = False
    cached: bool = False

    def __post_init__(self) -> None:
        if self.quality_report is None:
            self.quality_report = {}


@dataclass
class TranslationPipelineOutput:
    chunks: list
    results: list[TranslationResult]
    engine: object
