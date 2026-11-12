"""
术语 agent（#18）：用脚本化假模型走查证 → 提交、转交人工、预算降级三条路径；
结论必须引用查到过的段；用户术语表优先；关闭时与现状一致。
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from types import SimpleNamespace

from langchain_core.runnables import RunnableLambda

from src.application.tools.term_agent import term_agent_registry
from src.application.use_cases.term_agent_store import agent_entries, pending_reviews, save_report
from src.domain.rules.term_sources import SourceConfig, gather_candidates, paragraph_index
from src.domain.terminology.term_decisions import (
    DECIDED,
    HANDOFF,
    NOT_TERM,
    SenseDecision,
    TermDecision,
    check_conclusion,
    glossary_updates,
)
from src.infrastructure.agent import AgentBudget, ScriptedModel
from src.pipelines.glossary.term_agent import TermAgent, TermAgentReport, TermAgentSettings

TEXT = """Captain Mission sailed to Madagascar. The crew trusted Captain Mission.

Their mission was to found Libertatia. The mission failed within years.

Later, Mission wrote nothing; the mission of the Ccru was different. Captain Mission vanished.

The Ccru studied Mission and the mission of Libertatia again.
"""


def _index():
    return paragraph_index(TEXT)


def _candidates(index, glossary=None):
    config = SourceConfig(sources=("proper",), max_terms=120, contexts=2)
    return gather_candidates(index, glossary or {}, text=TEXT, cues=None, config=config)


def _mission(index):
    return next(c for c in _candidates(index) if c.term == "Mission")


def _positions(messages) -> list[int]:
    return [int(n) for n in re.findall(r"\[段 (\d+)\]", messages[-1]["content"])]


def _tool(name, **args):
    return json.dumps({"action": "tool", "tool": name, "args": args, "note": ""})


def _settings(**budget):
    return TermAgentSettings(enabled=True, budget=AgentBudget(**{"max_steps": 6, "max_tool_calls": 4, **budget}))


# --------------------------------------------------------------------------- #
# 结论校验与并入规则（纯逻辑）
# --------------------------------------------------------------------------- #

EVIDENCE = {"E1": {"tool": "find_occurrences", "args": {}, "output": "[段 0] … [段 2] …"}}


def test_conclusion_must_cite_segments_that_tools_returned():
    sense = {"rendering": "米申", "sense": "人名", "scope": "book", "positions": [0, 2]}
    assert check_conclusion({"decision": "term", "senses": [sense]}, EVIDENCE) is None
    unseen = {**sense, "positions": [0, 7]}
    assert "[7]" in check_conclusion({"decision": "term", "senses": [unseen]}, EVIDENCE)
    assert "scope" in check_conclusion({"decision": "term", "senses": [{**sense, "scope": "world"}]}, EVIDENCE)
    assert "rendering" in check_conclusion({"decision": "term", "senses": [{**sense, "rendering": ""}]}, EVIDENCE)
    assert check_conclusion({"decision": "not_term", "positions": [2]}, EVIDENCE) is None
    assert check_conclusion({"decision": "not_term", "positions": []}, EVIDENCE) is not None


def test_only_single_rendering_decisions_merge_and_user_glossary_wins():
    decisions = [
        TermDecision("Mission", DECIDED, [SenseDecision("米申", "人名", positions=(0,))]),
        TermDecision("Gates", DECIDED, [SenseDecision("盖茨", "人名"), SenseDecision("门", "数字图的门")]),
        TermDecision("Ccru", DECIDED, [SenseDecision("控制论文化研究小组", "组织")]),
        TermDecision("Libertatia", HANDOFF, reason="agent:拿不准"),
        TermDecision("Track", NOT_TERM, positions=(1,)),
    ]
    updates = glossary_updates(decisions, {"CCRU": "Ccru (CCRU)"}, {})
    assert updates == {"Mission": "米申"}  # 多义项、转交、普通词不并入；用户术语表里的 Ccru 不被覆盖


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def test_tools_mark_segments_and_page_through_occurrences():
    index = _index()
    registry = term_agent_registry(index=index, glossary={"Libertatia": "利伯塔利亚"}, ledger={"Ccru": "Ccru"})
    first = asyncio.run(registry.call("find_occurrences", {"term": "Mission"}))
    assert first.ok and "[段 0]" in first.output and "全书" in first.output
    looked = asyncio.run(registry.call("lookup_glossary", {"term": "Libertatia"}))
    assert "用户术语表" in looked.output and "利伯塔利亚" in looked.output
    searched = asyncio.run(registry.call("search_segments", {"query": "Captain Mission"}))
    assert "[段 0]" in searched.output and "[段 1]" not in searched.output
    pending = asyncio.run(registry.call("rendering_distribution", {"term": "Mission"}))
    assert "还没有译文" in pending.output
    bad = asyncio.run(registry.call("find_occurrences", {"word": "Mission"}))
    assert not bad.ok and "参数校验失败" in bad.output


def test_tools_come_from_shared_registry_but_stay_out_of_mcp():
    from src.application import tools
    from src.application.tools import term_agent

    assert term_agent not in tools.DEFAULT_MODULES  # 翻译前用的内存索引工具，不暴露给 MCP
    assert {spec.name for spec in tools.collect_specs(term_agent)} >= set(term_agent.TOOL_NAMES)
    plain = term_agent_registry(index=_index(), glossary={})
    assert "search_by_meaning" not in plain.names()
    meaning = term_agent_registry(index=_index(), glossary={}, search_by_meaning=lambda q, n: [(3, "The Ccru…", None)])
    hit = asyncio.run(meaning.call("search_by_meaning", {"query": "研究小组"}))
    assert hit.ok and "[段 3]" in hit.output


# --------------------------------------------------------------------------- #
# agent 运行：三条路径
# --------------------------------------------------------------------------- #


def test_verify_then_submit_splits_senses_with_traceable_evidence():
    index = _index()

    def submit(messages):
        positions = _positions(messages)
        person = [p for p in positions if "Captain Mission" in index.get(p).source]
        common = [p for p in positions if "the mission" in index.get(p).source.lower()]
        senses = [
            {"rendering": "米申", "sense": "人名：海盗 Captain Mission", "scope": "book", "positions": person},
            {"rendering": "使命", "sense": "普通词：任务", "scope": "book", "positions": common},
        ]
        return json.dumps(
            {"action": "submit", "conclusion": {"decision": "term", "senses": senses}, "evidence": ["E1"]}
        )

    model = ScriptedModel([_tool("find_occurrences", term="Mission"), submit])
    report = asyncio.run(TermAgent(model, _settings()).run([_mission(index)], index, {}, {}))
    decision = report.decisions[0]
    assert decision.status == DECIDED and decision.renderings == ["米申", "使命"]
    # 每个义项的证据段都在书里且含这个词
    assert decision.evidence_positions and all("ission" in index.get(p).source for p in decision.evidence_positions)
    assert decision.evidence and decision.evidence[0]["tool"] == "find_occurrences"
    assert report.updates({}, {}) == {}  # 多义项不作为全书统一译名
    assert report.usage()["steps"] == 2 and report.usage()["decided"] == 1


def test_submit_citing_unseen_segment_is_rejected_then_corrected():
    index = _index()
    bogus = {"decision": "term", "senses": [{"rendering": "米申", "sense": "人名", "positions": [99]}]}

    def good(messages):
        assert "没有出现在已查到的证据里" in messages[-1]["content"]
        conclusion = {"decision": "term", "senses": [{"rendering": "米申", "sense": "人名", "positions": [0]}]}
        return json.dumps({"action": "submit", "conclusion": conclusion, "evidence": ["E1"]})

    model = ScriptedModel(
        [
            _tool("search_segments", query="Captain Mission"),
            json.dumps({"action": "submit", "conclusion": bogus, "evidence": ["E1"]}),
            good,
        ]
    )
    report = asyncio.run(TermAgent(model, _settings()).run([_mission(index)], index, {}, {}))
    assert report.decisions[0].single_rendering == "米申"
    assert report.updates({}, {}) == {"Mission": "米申"}


def test_handoff_keeps_contexts_for_review():
    index = _index()
    model = ScriptedModel(
        [_tool("find_occurrences", term="Mission"), json.dumps({"action": "handoff", "reason": "人名与普通词混用"})]
    )
    report = asyncio.run(TermAgent(model, _settings()).run([_mission(index)], index, {}, {}))
    decision = report.decisions[0]
    assert decision.status == HANDOFF and "人名与普通词混用" in decision.reason
    assert decision.contexts and "Captain Mission" in decision.contexts[0]
    assert report.usage()["handoff_rate"] == 1.0


def test_budget_exhaustion_degrades_to_handoff():
    index = _index()
    model = ScriptedModel(
        [
            _tool("find_occurrences", term="Mission"),
            _tool("search_segments", query="mission"),
            _tool("lookup_glossary", term="Mission"),
        ]
    )
    report = asyncio.run(TermAgent(model, _settings(max_tool_calls=2)).run([_mission(index)], index, {}, {}))
    assert report.decisions[0].status == HANDOFF and report.decisions[0].reason == "budget:tool_calls"
    assert report.updates({}, {}) == {}


def test_terms_already_in_glossary_or_ledger_are_not_run():
    index = _index()
    model = ScriptedModel([])
    candidates = [_mission(index)]
    report = asyncio.run(TermAgent(model, _settings()).run(candidates, index, {"mission": "米申"}, {}))
    assert report.decisions == [] and model.calls == []


def test_report_round_trips_and_feeds_review_list(tmp_path):
    report = TermAgentReport(
        [
            TermDecision("Mission", DECIDED, [SenseDecision("米申", "人名", "author", (0, 2))], tokens=300, steps=3),
            TermDecision("Gates", HANDOFF, reason="agent:门还是人名", contexts=["…the Gates of the Numogram…"]),
        ]
    )
    assert TermAgentReport.from_dict(json.loads(json.dumps(report.to_dict()))).to_dict() == report.to_dict()
    task = SimpleNamespace(task_id="t1", filename="Ccru.epub", author=None, domain=None)
    save_report(tmp_path, task, report)
    reviews = pending_reviews(tmp_path)
    assert [item["term"] for item in reviews] == ["Gates"] and reviews[0]["contexts"]
    entries = agent_entries(task, report)
    # 作者范围只在任务填了作者时生效；条目带义项与证据段，来源记为术语 agent
    assert [(e.term, e.scope, e.source, e.evidence) for e in entries] == [("Mission", "book", "agent", (0, 2))]


# --------------------------------------------------------------------------- #
# 接入翻译引擎：关闭时与现状一致，开启时只处理有歧义的候选
# --------------------------------------------------------------------------- #


class _Analyst:
    def __init__(self):
        self.ledger_prompts: list[str] = []
        self.agent_calls = 0

    def __call__(self, value):
        if isinstance(value, list):  # 术语 agent：消息列表
            self.agent_calls += 1
            if self.agent_calls == 1:
                return _tool("find_occurrences", term="Mission")
            return json.dumps({"action": "handoff", "reason": "测试"})
        text = value.to_string()
        if "【候选术语】" in text:
            self.ledger_prompts.append(text)
            terms = re.findall(r"^- (\S+(?: \S+)?)（", text, re.M)
            return json.dumps({"terms": [{"term": t, "translation": f"译{t}", "keep": True} for t in terms]})
        return '{"summary": "测试"}'


def _engine(tmp_path, analyst, agent_enabled: bool):
    from src.core.translator import TranslationEngine
    from src.infrastructure.cache.translation_cache import TranslationCache
    from src.utils.config_loader import get_config

    config = copy.deepcopy(get_config("config/config.yaml"))  # 不改动全局单例
    config.config["multi_agent"]["term_agent"]["enabled"] = agent_enabled

    class Factory:
        def create_translator(self):
            return RunnableLambda(lambda _: "")

        def create_checker(self):
            return RunnableLambda(lambda _: "")

        def create_analyst(self):
            return RunnableLambda(analyst)

    return TranslationEngine(
        model_factory=Factory(), cache=TranslationCache(tmp_path / "cache.db"), config=config, engine="native"
    )


def test_engine_with_agent_off_behaves_as_before(tmp_path):
    analyst = _Analyst()
    engine = _engine(tmp_path, analyst, agent_enabled=False)
    asyncio.run(engine._prepare_term_ledger(TEXT))
    assert analyst.agent_calls == 0 and engine.term_agent_report is None
    assert "Mission" in engine.term_ledger  # 有歧义的候选仍走一次性账本


def test_engine_with_agent_on_defers_ambiguous_candidates(tmp_path):
    analyst = _Analyst()
    engine = _engine(tmp_path, analyst, agent_enabled=True)
    asyncio.run(engine._prepare_term_ledger(TEXT))
    assert all("- Mission（" not in prompt for prompt in analyst.ledger_prompts)
    report = engine.term_agent_report
    deferred = sorted(d.term for d in report.handoffs)
    assert deferred == ["Captain Mission", "Mission"]  # "Captain Mission" 也被标为可能只是句首大写
    assert "Mission" not in engine.term_ledger and "Libertatia" in engine.term_ledger  # 无歧义的照常走账本
    assert report.usage()["model_calls"] == analyst.agent_calls == 3

    # 第二次运行命中账本缓存，agent 的结果随缓存恢复，不再调用模型
    again = _engine(tmp_path, analyst, agent_enabled=True)
    asyncio.run(again._prepare_term_ledger(TEXT))
    assert analyst.agent_calls == 3 and sorted(d.term for d in again.term_agent_report.handoffs) == deferred


# --------------------------------------------------------------------------- #
# 对比基准：一次性调用 vs agent
# --------------------------------------------------------------------------- #


def test_bench_scores_misjudgment_handoff_and_cost(tmp_path):
    from src.evaluation.term_agent_bench import BenchItem, main, misjudged

    candidate = SimpleNamespace(term="Mission", ambiguity=["mixed_case"])
    assert misjudged(BenchItem("Mission", candidate, wrong=("使命",)), "使命")
    assert not misjudged(BenchItem("Mission", candidate, wrong=("使命",)), None)
    assert not misjudged(BenchItem("Horde", candidate, gold="部落/游牧群 (Horde)"), "部落")
    assert misjudged(BenchItem("Sun", candidate, gold="太阳"), "孙")

    source = tmp_path / "Ccru.md"
    source.write_text(TEXT, encoding="utf-8")
    misjudgments = tmp_path / "m.json"
    misjudgments.write_text(json.dumps({"items": [{"book": "Ccru", "term": "Mission", "wrong": ["使命"]}]}))

    def llm(value):
        if isinstance(value, list):  # agent：先查，再按人名提交
            if "[段 " not in value[-1].content:
                return _tool("search_segments", query="Captain Mission")
            conclusion = {"decision": "term", "senses": [{"rendering": "米申", "sense": "人名", "positions": [0]}]}
            return json.dumps({"action": "submit", "conclusion": conclusion, "evidence": ["E1"]})
        return json.dumps({"terms": [{"term": "Mission", "translation": "使命", "keep": True}]})

    out = tmp_path / "out"
    argv = ["--source", str(source), "--book", "Ccru", "--misjudgments", str(misjudgments), "--out", str(out)]
    assert main(argv, model_builder=lambda _config: (RunnableLambda(llm), "fake")) == 0
    report = json.loads((out / "term_agent_bench.json").read_text(encoding="utf-8"))
    assert report["items"] == 1
    assert report["one_shot"]["misjudged_terms"] == ["Mission"] and report["agent"]["misjudged"] == 0
    assert report["agent"]["mean_steps"] == 2 and report["agent"]["tokens"] > 0
    assert "一次性调用" in (out / "term_agent_bench.md").read_text(encoding="utf-8")
