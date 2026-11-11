"""评测框架的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field

# 三个打分维度：准确性、流畅度、术语一致性
DIMENSIONS = ("accuracy", "fluency", "terminology")
DIMENSION_LABELS = {
    "accuracy": "准确性",
    "fluency": "流畅度",
    "terminology": "术语一致性",
}

# 打分区间（1~5 的 Likert 量表）
SCORE_MIN = 1.0
SCORE_MAX = 5.0
# 准确性不高于此分视为严重误译，综合分以准确性封顶
SEVERE_ACCURACY = 2.0


@dataclass(frozen=True)
class EvalSample:
    """评测数据集里的一条摘选。"""

    id: str
    kind: str  # philosophy | paper
    source_text: str
    glossary: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DimensionScores:
    """裁判在三个维度上的打分。"""

    accuracy: float
    fluency: float
    terminology: float

    @property
    def overall(self) -> float:
        """综合分：三维平均，但准确性 ≤ SEVERE_ACCURACY 时不超过准确性分。

        严重误译（否定反转、数字写反）时译文可能依旧通顺，单纯平均会让流畅度把它拉回中等。
        """
        mean = round((self.accuracy + self.fluency + self.terminology) / 3, 3)
        return min(mean, self.accuracy) if self.accuracy <= SEVERE_ACCURACY else mean

    def as_dict(self) -> dict[str, float]:
        return {
            "accuracy": self.accuracy,
            "fluency": self.fluency,
            "terminology": self.terminology,
            "overall": self.overall,
        }


@dataclass(frozen=True)
class JudgeVerdict:
    """裁判对单条译文的裁决。

    裁判输出缺维度、分值非法或无法解析时 scores 为 None，记为无效裁决：
    不参与均值，单独计数，避免把"没评出来"当成中等表现。
    """

    sample_id: str
    scores: DimensionScores | None
    rationale: str = ""
    invalid_reason: str = ""

    @property
    def valid(self) -> bool:
        return self.scores is not None


@dataclass
class VariantRun:
    """某个对照变体在单条样本上的一次翻译产出（含成本指标）。"""

    variant: str
    sample_id: str
    translation: str
    total_tokens: int = 0
    latency_s: float = 0.0
    call_count: int = 0


@dataclass
class VariantAggregate:
    """某个对照变体在整个数据集上的汇总。"""

    variant: str
    label: str
    n: int
    mean_accuracy: float
    mean_fluency: float
    mean_terminology: float
    mean_overall: float
    total_tokens: int
    mean_latency_s: float
    invalid_judgments: int = 0

    def as_dict(self) -> dict:
        return {
            "variant": self.variant,
            "label": self.label,
            "n": self.n,
            "invalid_judgments": self.invalid_judgments,
            "mean_accuracy": self.mean_accuracy,
            "mean_fluency": self.mean_fluency,
            "mean_terminology": self.mean_terminology,
            "mean_overall": self.mean_overall,
            "total_tokens": self.total_tokens,
            "mean_latency_s": self.mean_latency_s,
        }


@dataclass
class ComparisonGroup:
    """一组对照（如 langgraph vs native）的汇总结果。"""

    name: str  # engine | multi_agent | glossary
    title: str
    variants: list[VariantAggregate]

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "variants": [v.as_dict() for v in self.variants],
        }
