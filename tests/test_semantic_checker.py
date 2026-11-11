"""语义质检：确定性启发式、模型检查（假模型）、配置开关与结构化翻译流程中的接入。"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from src.domain.rules import semantic_signals as signals
from src.infrastructure.document import pandoc
from src.pipelines.translate.semantic_checker import SegmentPair, SemanticChecker, findings_to_issues, parse_findings


def _kinds(source: str, translation: str) -> list[str]:
    return [finding.kind for finding in signals.heuristic_findings(source, translation)]


# ---------- 启发式 ----------


def test_correct_translations_raise_nothing():
    assert _kinds("The method does not improve accuracy.", "该方法没有提高准确率。") == []
    assert _kinds("The result may suggest a correlation.", "该结果可能表明存在相关性。") == []
    assert _kinds("Accuracy rose from 70 to 80.", "准确率从 70 提升至 80。") == []
    assert (
        _kinds("The method improves accuracy but doubles memory use.", "该方法提高了准确率，但内存占用翻了一倍。") == []
    )


def test_negation_dropped_or_reduced():
    assert _kinds("The method does not improve accuracy.", "该方法提高了准确率。") == ["negation"]
    reduced = signals.check_negation("It is not red and never blue.", "它不是红色，总是蓝色。")
    assert reduced and reduced[0].severity == "low"
    # "不断""无论"不算否定
    assert signals.negation_counts("It never stops.", "它不断运转，无论如何。") == (1, 0)


def test_modality_strengthened_and_weakened():
    assert "modality" in _kinds("The result may suggest a correlation.", "该结果证明了因果关系。")
    weakened = signals.check_modality("Every submission must include the script.", "每份提交可以附上脚本。")
    assert weakened and weakened[0].severity == "low"


def test_number_direction_and_trend():
    assert "number_role" in _kinds("Accuracy rose from 70 to 80.", "准确率从 80 提升至 70。")
    assert "number_role" in _kinds("Between 1987 and 1993 it grew.", "1993 年至 1987 年间它增长了。")
    assert "number_role" in _kinds("Accuracy rose from 70 to 80.", "准确率从 70 降至 80。")


def test_omission_by_length_and_contrast():
    assert "omission" in _kinds(
        "The method improves accuracy on every benchmark but doubles memory use during training.",
        "该方法提高了准确率。",
    )
    assert signals.check_contrast("Good but slow.", "很好。")


def test_code_and_links_are_ignored_by_semantic_signals():
    assert signals.risk_factors("`x = not y`") == []
    assert signals.risk_factors("See [it](https://e.org/1/2).") == []


def test_risk_factors():
    assert signals.risk_factors("It may not rise from 3 to 4.") == ["negation", "modality", "number"]
    assert signals.risk_factors("A plain sentence.") == []
    assert "long" in signals.risk_factors(" ".join(["word"] * 40) + ".")


# ---------- 模型检查 ----------


def _fake_model(findings_by_id, calls):
    def run(prompt):
        text = prompt if isinstance(prompt, str) else prompt.to_string()
        ids = [int(i) for i in re.findall(r"【段 (\d+)】", text)]
        calls.append(ids)
        findings = [finding for key, finding in findings_by_id.items() if key in ids]
        return AIMessage(
            content=json.dumps({"findings": findings}, ensure_ascii=False),
            usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        )

    return RunnableLambda(run)


def test_model_findings_are_structured_and_high_risk_routing_limits_calls():
    calls = []
    model = _fake_model(
        {
            1: {
                "id": 1,
                "type": "negation",
                "severity": "high",
                "source_span": "not",
                "target_span": "提高",
                "reason": "否定丢失",
            },
            2: {"id": 2, "type": "weird", "severity": "high", "reason": "说不准"},
        },
        calls,
    )
    checker = SemanticChecker(model, use_model=True, high_risk_only=True)
    pairs = [
        SegmentPair(1, "It does not help.", "它有帮助。"),
        SegmentPair(2, "It may help.", "它可能有帮助。"),
        SegmentPair(3, "A plain sentence.", "一个普通的句子。"),
    ]
    result = asyncio.run(checker.check(pairs))
    assert calls == [[1, 2]]  # 第 3 段没有高风险信号，不送模型
    model_findings = [f for f in result.findings[1] if f.origin == "model"]
    assert model_findings[0].kind == "negation" and model_findings[0].severity == "high"
    assert model_findings[0].source_span == "not" and model_findings[0].target_span == "提高"
    uncertain = result.findings[2][0]
    assert (uncertain.kind, uncertain.severity) == ("uncertain", "low")  # 未知类型归为无法判定
    assert result.usage.as_dict() | {} == {
        "segments_total": 3,
        "segments_sent": 2,
        "calls": 1,
        "input_tokens": 100,
        "output_tokens": 20,
        "tokens_estimated": False,
        "invalid_responses": 0,
        "seconds": result.usage.seconds,
    }

    calls.clear()
    asyncio.run(SemanticChecker(model, use_model=True, high_risk_only=False).check(pairs))
    assert calls == [[1, 2, 3]]


def test_unparseable_model_output_is_counted_not_raised():
    checker = SemanticChecker(RunnableLambda(lambda _p: "无法回答"), use_model=True, high_risk_only=False)
    result = asyncio.run(checker.check([SegmentPair(1, "Fine.", "很好。")]))
    assert result.findings == {} and result.usage.invalid_responses == 1
    assert result.usage.tokens_estimated is True and result.usage.input_tokens > 0


def test_parse_findings_drops_unknown_segments():
    raw = '{"findings": [{"id": 9, "type": "negation"}, {"id": 1, "type": "omission", "severity": "medium"}]}'
    parsed = parse_findings(raw, {1})
    assert [(key, f.kind) for key, f in parsed] == [(1, "omission")]


def test_issues_are_labelled_as_hints():
    issues = findings_to_issues(signals.check_negation("It is not.", "它是。"), segment=7)
    assert issues[0].kind == "semantic" and issues[0].segment == 7
    assert issues[0].message.startswith("[语义提示·规则] 否定")


def test_config_switch():
    from src.utils.config_loader import Config

    config = Config(Path("config/config.yaml"))
    assert config.get("quality.semantic_check.enabled") is False
    assert SemanticChecker.from_config(config) is None


# ---------- 接入结构化翻译 ----------


def _segments(prompt_value) -> list[tuple[str, str]]:
    body = prompt_value.to_string().split("【待翻译段落】:")[-1]
    return re.findall(r'<seg id="(\d+)">(.*?)</seg>', body, re.S)


_TEXT = "The method does not improve accuracy.\n\nA plain sentence here.\n"


def _drop_negation(prompt_value) -> str:
    out = []
    for local_id, text in _segments(prompt_value):
        out.append(f'<seg id="{local_id}">{"该方法提高了准确率。" if "not" in text else "这是一个普通的句子。"}</seg>')
    return "\n".join(out)


class _Factory:
    def __init__(self, semantic_calls):
        self.semantic_calls = semantic_calls

    def create_translator(self):
        return RunnableLambda(_drop_negation)

    def create_checker(self):
        return RunnableLambda(lambda _p: "")

    def create_analyst(self):
        return RunnableLambda(lambda _p: '{"summary": "测试"}')

    def create_semantic_checker(self):
        return _fake_model(
            {1: {"id": 1, "type": "negation", "severity": "high", "reason": "否定被反转"}}, self.semantic_calls
        )


def _run(tmp_path, semantic: dict | None):
    from src.core.translator import TranslationEngine
    from src.infrastructure.cache.translation_cache import TranslationCache
    from src.utils.config_loader import Config

    config = Config(Path("config/config.yaml"))
    if semantic is not None:
        config.config["quality"]["semantic_check"].update(semantic)
    calls = []
    engine = TranslationEngine(
        model_factory=_Factory(calls), cache=TranslationCache(tmp_path / "cache.db"), config=config, engine="native"
    )
    chunks = engine.plan_chunks(_TEXT)
    results = asyncio.run(engine.translate_batch(_TEXT, tmp_path / "stream.md", prepared_chunks=chunks))
    return results[0], calls, engine


@pytest.mark.skipif(not pandoc.pandoc_available(), reason="需要 pandoc")
def test_switched_off_pipeline_is_unchanged(tmp_path):
    result, calls, engine = _run(tmp_path, None)
    assert engine.semantic_checker is None and calls == []
    assert not any(issue["kind"] == "semantic" for issue in result.quality_report["issues"])


@pytest.mark.skipif(not pandoc.pandoc_available(), reason="需要 pandoc")
def test_semantic_hints_enter_the_report_without_failing_the_chunk(tmp_path):
    result, calls, engine = _run(tmp_path, {"enabled": True, "use_model": True, "high_risk_only": True})
    semantic = [issue for issue in result.quality_report["issues"] if issue["kind"] == "semantic"]
    messages = " ".join(issue["message"] for issue in semantic)
    assert "[语义提示·规则] 否定" in messages and "[语义提示·模型] 否定" in messages
    assert all("segment" in issue for issue in semantic)
    assert calls == [[1]]  # 只有含否定的那段送模型
    assert result.quality_report["passed"] is True  # 语义提示只报告，不决定通过与否
    assert engine.semantic_checker.usage.calls == 1 and engine.semantic_checker.usage.input_tokens == 100
