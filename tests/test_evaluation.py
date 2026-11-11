"""
评测框架单元测试

按 PRD 的测试接缝：注入符合 Runnable 协议的假裁判验证打分解析与汇总；用合成对齐语料
验证一致率与 RAG 门槛决策。全部不触网、不需密钥。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.runnables import RunnableLambda

from src.evaluation import (
    AlignedSegment,
    DimensionScores,
    EvalSample,
    JudgeVerdict,
    TranslationJudge,
    VariantRun,
    aggregate_variant,
    build_comparison,
    compute_consistency,
    parse_scores,
)
from src.evaluation.judge import InvalidVerdictError
from src.evaluation.report import CostSummary, EvalReport, render_comparison_table, render_report

# ---------- parse_scores ----------


def test_parse_scores_plain_json():
    scores = parse_scores('{"accuracy": 5, "fluency": 4, "terminology": 3, "rationale": "ok"}')
    assert (scores.accuracy, scores.fluency, scores.terminology) == (5.0, 4.0, 3.0)
    assert scores.overall == 4.0


def test_parse_scores_fenced_and_surrounding_text():
    raw = '这是我的评价：\n```json\n{"accuracy": 4, "fluency": 5, "terminology": 4}\n```\n完毕'
    scores = parse_scores(raw)
    assert (scores.accuracy, scores.fluency, scores.terminology) == (4.0, 5.0, 4.0)


def test_parse_scores_rejects_out_of_range():
    with pytest.raises(InvalidVerdictError):
        parse_scores('{"accuracy": 9, "fluency": 0, "terminology": 3}')


def test_parse_scores_missing_dimension_is_invalid_not_midpoint():
    with pytest.raises(InvalidVerdictError, match="terminology"):
        parse_scores('{"accuracy": 4, "fluency": 5}')
    with pytest.raises(InvalidVerdictError):
        parse_scores("{}")
    with pytest.raises(InvalidVerdictError):
        parse_scores('{"accuracy": "good", "fluency": 5, "terminology": 5}')


def test_overall_does_not_let_fluency_mask_severe_mistranslation():
    assert DimensionScores(1, 5, 5).overall == 1.0
    assert DimensionScores(2, 5, 5).overall == 2.0
    assert DimensionScores(4, 5, 3).overall == 4.0


def test_empty_judge_output_yields_invalid_verdict_that_is_counted():
    judge = TranslationJudge(RunnableLambda(lambda _p: "{}"), "{source}{translation}{glossary}")
    verdict = asyncio.run(judge.score(EvalSample("s1", "paper", "src"), "译文"))
    assert verdict.valid is False and verdict.scores is None and "缺少维度" in verdict.invalid_reason

    runs = [VariantRun("v", "s1", "t1"), VariantRun("v", "s2", "t2")]
    agg = aggregate_variant("v", "V", runs, [verdict, JudgeVerdict("s2", DimensionScores(4, 4, 4))])
    assert agg.n == 1 and agg.invalid_judgments == 1
    assert agg.mean_overall == 4.0  # 无效裁决没有被当成 3 分拉低均值


def test_parse_scores_without_json_raises():
    with pytest.raises(ValueError):
        parse_scores("完全没有 JSON 的一段话")


# ---------- TranslationJudge ----------


def _sample() -> EvalSample:
    return EvalSample(id="s1", kind="paper", source_text="reward model", glossary={"reward model": "奖励模型"})


def test_judge_scores_from_string_runnable():
    judge = TranslationJudge(
        RunnableLambda(lambda _p: '{"accuracy": 4, "fluency": 4, "terminology": 5, "rationale": "术语准确"}'),
        prompt_template="原文:{source}\n译文:{translation}\n术语:{glossary}",
    )
    verdict = asyncio.run(judge.score(_sample(), "奖励模型"))
    assert isinstance(verdict, JudgeVerdict)
    assert verdict.scores.terminology == 5.0
    assert verdict.rationale == "术语准确"
    assert verdict.sample_id == "s1"


def test_judge_reads_message_content_object():
    judge = TranslationJudge(
        RunnableLambda(lambda _p: SimpleNamespace(content='{"accuracy": 3, "fluency": 3, "terminology": 3}')),
        prompt_template="{source}{translation}{glossary}",
    )
    verdict = asyncio.run(judge.score(_sample(), "奖励模型"))
    assert verdict.scores.overall == 3.0


def test_judge_prompt_fills_placeholders():
    judge = TranslationJudge(RunnableLambda(lambda p: p), prompt_template="S={source}|T={translation}|G={glossary}")
    prompt = judge.build_prompt(_sample(), "奖励模型")
    assert "S=reward model" in prompt
    assert "T=奖励模型" in prompt
    assert "reward model: 奖励模型" in prompt


# ---------- aggregation ----------


def test_aggregate_variant_means_and_pairing():
    runs = [
        VariantRun("langgraph", "s1", "t1", total_tokens=100, latency_s=2.0),
        VariantRun("langgraph", "s2", "t2", total_tokens=150, latency_s=4.0),
        VariantRun("langgraph", "s3", "t3", total_tokens=999, latency_s=9.0),  # 无对应裁决 → 忽略
    ]
    verdicts = [
        JudgeVerdict("s1", DimensionScores(4, 4, 4)),
        JudgeVerdict("s2", DimensionScores(2, 2, 2)),
        JudgeVerdict("sX", DimensionScores(5, 5, 5)),  # 无对应产出 → 忽略
    ]
    agg = aggregate_variant("langgraph", "LangGraph", runs, verdicts)
    assert agg.n == 2
    assert agg.mean_accuracy == 3.0
    assert agg.mean_overall == 3.0
    assert agg.total_tokens == 250  # 只计入配对样本
    assert agg.mean_latency_s == 3.0


def test_build_comparison_groups_variants():
    agg = aggregate_variant("langgraph", "LangGraph", [], [])
    group = build_comparison("engine", "引擎对比", [agg])
    assert group.name == "engine"
    assert group.variants[0].variant == "langgraph"


# ---------- consistency / RAG 门槛 ----------


def _corpus() -> list[AlignedSegment]:
    return [
        AlignedSegment("The reward model scores the answer", "奖励模型给答案打分"),
        AlignedSegment("A reward model needs training", "奖励模型需要训练"),
        AlignedSegment("The flat line appears", "平线出现"),
        AlignedSegment("Another flat line here", "零度在此"),
    ]


def test_consistency_mixed_rate_and_decision():
    report = compute_consistency(_corpus(), glossary={}, threshold=0.90)
    phrases = {p.phrase: p for p in report.phrases}
    assert phrases["reward model"].consistent is True
    assert phrases["reward model"].signature == "奖励模型"
    assert phrases["flat line"].consistent is False
    assert report.total_phrases == 2
    assert report.rate == 0.5
    assert report.decision == "build-rag"  # 0.5 < 0.9


def test_consistency_glossary_excludes_covered_phrase():
    report = compute_consistency(_corpus(), glossary={"reward model": "奖励模型"}, threshold=0.90)
    phrases = {p.phrase for p in report.phrases}
    assert "reward model" not in phrases  # 被术语表覆盖，不计入
    assert "flat line" in phrases
    assert report.total_phrases == 1


def test_consistency_empty_corpus_is_insufficient_data():
    report = compute_consistency([], glossary={}, threshold=0.90)
    assert report.rate is None
    assert report.decision == "insufficient-data"


def test_consistency_skips_function_word_grams():
    report = compute_consistency(_corpus(), glossary={}, threshold=0.90)
    phrases = {p.phrase for p in report.phrases}
    # 首尾为停用词的 n-gram 不应成为候选
    assert "the answer" not in phrases
    assert "the flat" not in phrases


# ---------- report 渲染 ----------


def test_render_comparison_table_contains_labels():
    agg = aggregate_variant(
        "langgraph",
        "LangGraph",
        [VariantRun("langgraph", "s1", "t", total_tokens=10, latency_s=1.0)],
        [JudgeVerdict("s1", DimensionScores(4, 4, 4))],
    )
    table = render_comparison_table(build_comparison("engine", "引擎对比", [agg]))
    assert "引擎对比" in table
    assert "LangGraph" in table
    assert "| 变体 |" in table


def test_render_report_includes_decision_and_cost():
    agg = aggregate_variant(
        "langgraph",
        "LangGraph",
        [VariantRun("langgraph", "s1", "t", total_tokens=10, latency_s=1.0)],
        [JudgeVerdict("s1", DimensionScores(4, 4, 4))],
    )
    report = EvalReport(
        comparisons=[build_comparison("engine", "引擎对比", [agg])],
        consistency=compute_consistency(_corpus(), glossary={}, threshold=0.90),
        cost=CostSummary(total_tokens=1234, total_calls=7, estimated_cost_rmb=3.5),
        meta={"judge_model": "gemini", "candidate_model": "deepseek"},
    )
    text = render_report(report)
    assert "翻译质量评测报告" in text
    assert "立项文档内翻译记忆" in text  # build-rag 决策文案
    assert "1234" in text
    assert "3.50 元" in text


def test_unrelated_shared_sentence_no_longer_makes_phrase_consistent():
    segments = [
        AlignedSegment("The buried engine awakens. This is a shared note.", "埋藏引擎醒来。这是一条共同注释。"),
        AlignedSegment("A buried engine stops. This is a shared note.", "地下发动机停转。这是一条共同注释。"),
    ]
    report = compute_consistency(segments)
    phrases = {p.phrase: p for p in report.phrases}
    assert phrases["buried engine"].consistent is False
    assert phrases["shared note"].consistent is True
    assert report.aligned_units == 4 and report.unaligned_units == 0


def test_consistency_skips_paragraphs_whose_sentences_do_not_align():
    segments = [
        AlignedSegment("The buried engine awakens. It hums.", "埋藏引擎醒来并嗡嗡作响。"),
        AlignedSegment("A buried engine stops.", "埋藏引擎停转。"),
    ]
    report = compute_consistency(segments)
    assert report.unaligned_units == 1 and report.total_phrases == 0
