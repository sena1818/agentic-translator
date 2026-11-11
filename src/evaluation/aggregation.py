"""把逐样本的打分与成本汇总成对照变体的均值。"""

from __future__ import annotations

from collections.abc import Sequence

from .models import ComparisonGroup, JudgeVerdict, VariantAggregate, VariantRun


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def aggregate_variant(
    variant: str,
    label: str,
    runs: Sequence[VariantRun],
    verdicts: Sequence[JudgeVerdict],
) -> VariantAggregate:
    """按样本对齐运行产出与裁判裁决，汇总成变体级均值。

    以 sample_id 对齐；只统计既有译文产出又有有效裁决的样本，避免半条数据污染均值。
    无效裁决（裁判缺维度、分值非法）单独计数，不参与均值。
    token 取总量（体现整组成本），延迟取均值（体现单条体验）。
    """
    verdict_by_sample = {v.sample_id: v for v in verdicts}
    matched = [(run, verdict_by_sample[run.sample_id]) for run in runs if run.sample_id in verdict_by_sample]
    paired = [(run, verdict) for run, verdict in matched if verdict.valid]

    accuracies = [verdict.scores.accuracy for _run, verdict in paired]
    fluencies = [verdict.scores.fluency for _run, verdict in paired]
    terminologies = [verdict.scores.terminology for _run, verdict in paired]
    overalls = [verdict.scores.overall for _run, verdict in paired]
    latencies = [run.latency_s for run, _verdict in paired]
    total_tokens = sum(run.total_tokens for run, _verdict in paired)

    return VariantAggregate(
        variant=variant,
        label=label,
        n=len(paired),
        mean_accuracy=_mean(accuracies),
        mean_fluency=_mean(fluencies),
        mean_terminology=_mean(terminologies),
        mean_overall=_mean(overalls),
        total_tokens=total_tokens,
        mean_latency_s=_mean(latencies),
        invalid_judgments=len(matched) - len(paired),
    )


def build_comparison(name: str, title: str, variants: Sequence[VariantAggregate]) -> ComparisonGroup:
    """把同一对照组的多个变体汇总组装成对照结果。"""
    return ComparisonGroup(name=name, title=title, variants=list(variants))
