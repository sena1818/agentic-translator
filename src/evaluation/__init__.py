"""
翻译质量评测框架

三步证据链（见 issue #6 / docs/evals/methodology.md）：

1. 用异源强模型（Gemini）做 LLM-as-judge，对译文按准确性/流畅度/术语一致性打分；
2. 两组对照——多 Agent 开 vs 关、术语表有 vs 无——各出分数表 + token/延迟；
3. 统计术语表外重复短语的译法一致率，据 0.90 门槛决定是否立项 RAG 翻译记忆。

设计原则：打分解析、聚合、一致率计算都是纯函数，注入假裁判 Runnable 即可单元验证，
不触网、不需密钥；真实评测由 runner/cli 在具备密钥时驱动。
"""
from .aggregation import aggregate_variant, build_comparison
from .consistency import (
    AlignedSegment,
    ConsistencyReport,
    PhraseConsistency,
    compute_consistency,
)
from .judge import TranslationJudge, parse_scores
from .models import (
    DIMENSIONS,
    SCORE_MAX,
    SCORE_MIN,
    ComparisonGroup,
    DimensionScores,
    EvalSample,
    JudgeVerdict,
    VariantAggregate,
    VariantRun,
)

__all__ = [
    "DIMENSIONS",
    "SCORE_MIN",
    "SCORE_MAX",
    "EvalSample",
    "DimensionScores",
    "JudgeVerdict",
    "VariantRun",
    "VariantAggregate",
    "ComparisonGroup",
    "TranslationJudge",
    "parse_scores",
    "aggregate_variant",
    "build_comparison",
    "AlignedSegment",
    "PhraseConsistency",
    "ConsistencyReport",
    "compute_consistency",
]
