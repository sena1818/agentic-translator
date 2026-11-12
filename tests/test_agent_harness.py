"""
手写 agent 运行框架：用脚本化的假模型驱动，只断言对外行为（结局、模型看到的上下文、落盘与回放）。
"""

from __future__ import annotations

import asyncio
import json
from typing import TypedDict

import pytest

from src.infrastructure.agent import (
    AgentBudget,
    AgentItem,
    AgentRunner,
    Checkpoint,
    JsonlTracer,
    MemoryTracer,
    ModelReply,
    RunnableChatModel,
    ScriptedModel,
    ToolRegistry,
    as_graph_node,
    replay,
)


def tool(name, args, note=""):
    return json.dumps({"thought": "查", "note": note, "action": "tool", "tool": name, "args": args}, ensure_ascii=False)


def submit(conclusion, evidence, note=""):
    payload = {"thought": "够了", "note": note, "action": "submit", "conclusion": conclusion, "evidence": evidence}
    return json.dumps(payload, ensure_ascii=False)


def handoff(reason):
    return json.dumps({"thought": "拿不准", "action": "handoff", "reason": reason}, ensure_ascii=False)


TERM_SCHEMA = {
    "type": "object",
    "properties": {"term": {"type": "string"}},
    "required": ["term"],
    "additionalProperties": False,
}


def make_registry(output_size: int = 0):
    registry = ToolRegistry()
    calls = []

    @registry.tool("lookup", "查一个词在全书的出现处", TERM_SCHEMA)
    def lookup(term):
        calls.append(term)
        if output_size:
            return f"{term}:" + "x" * output_size
        return f"{term} 出现 3 次：第 1、4、9 章"

    @registry.tool("broken", "总是出错的工具", TERM_SCHEMA)
    def broken(term):
        raise RuntimeError("索引不可用")

    return registry, calls


def run(runner, item_id="t1", task="给 Mission 定译名", decided=""):
    return asyncio.run(runner.run(AgentItem(item_id, task), decided))


def last_observation(messages):
    return messages[-1]["content"]


# --------------------------------------------------------------------------- #
# 四条路径
# --------------------------------------------------------------------------- #


def test_normal_path_submits_conclusion_with_cited_evidence():
    registry, calls = make_registry()
    model = ScriptedModel([tool("lookup", {"term": "Mission"}), submit({"zh": "使命"}, ["E1"], note="多指宗教使命")])
    outcome = run(AgentRunner(model, registry, instructions="你是术语 agent"))
    assert outcome.status == "submitted" and outcome.conclusion == {"zh": "使命"}
    assert [e["id"] for e in outcome.evidence] == ["E1"] and "出现 3 次" in outcome.evidence[0]["excerpt"]
    assert (outcome.steps, outcome.tool_calls) == (2, 1) and calls == ["Mission"]
    assert "[E1]" in last_observation(model.calls[1])


def test_tool_errors_come_back_as_observations_and_the_agent_recovers():
    registry, _ = make_registry()
    seen = []

    def remember(reply):
        def step(messages):
            seen.append(last_observation(messages))
            return reply

        return step

    model = ScriptedModel(
        [
            tool("lookup", {"word": "Mission"}),  # 参数不合 schema
            remember(tool("search", {"term": "Mission"})),  # 不存在的工具
            remember(tool("broken", {"term": "Mission"})),  # 工具抛错
            remember("我觉得应该译成使命"),  # 不合协议
            remember(tool("lookup", {"term": "Mission"})),
            submit("使命", ["E3"]),
        ]
    )
    outcome = run(AgentRunner(model, registry, instructions="x"))
    assert outcome.status == "submitted" and outcome.conclusion == "使命"
    assert "参数校验失败" in seen[0] and "缺少必填参数 term" in seen[0] and "不接受参数 word" in seen[0]
    assert "没有名为 search 的工具" in seen[1]
    assert "执行出错" in seen[2] and "索引不可用" in seen[2]
    assert "不符合输出协议" in seen[3]


def test_repeated_identical_call_is_warned_then_terminated():
    registry, calls = make_registry()
    model = ScriptedModel([tool("lookup", {"term": "Gates"})] * 3)
    outcome = run(AgentRunner(model, registry, instructions="x"))
    assert outcome.status == "handoff" and outcome.reason == "loop:重复调用 lookup"
    assert calls == ["Gates"]  # 重复调用不再执行
    assert "相同参数" in last_observation(model.calls[2]) and "E1" in last_observation(model.calls[2])


@pytest.mark.parametrize(
    ("budget", "tokens", "reason"),
    [
        (AgentBudget(max_steps=3), None, "budget:steps"),
        (AgentBudget(max_tool_calls=2), None, "budget:tool_calls"),
        (AgentBudget(max_tokens=2500), 1000, "budget:tokens"),
    ],
)
def test_exceeding_any_budget_degrades_to_handoff(budget, tokens, reason):
    registry, calls = make_registry()
    model = ScriptedModel([tool("lookup", {"term": f"t{i}"}) for i in range(10)], tokens=tokens)
    outcome = run(AgentRunner(model, registry, instructions="x", budget=budget))
    assert outcome.status == "handoff" and outcome.reason == reason
    assert outcome.steps <= budget.max_steps and outcome.tool_calls <= budget.max_tool_calls


def test_agent_can_hand_off_on_its_own():
    registry, _ = make_registry()
    outcome = run(AgentRunner(ScriptedModel([handoff("两种义项都说得通")]), registry, instructions="x"))
    assert outcome.status == "handoff" and outcome.reason == "agent:两种义项都说得通"


# --------------------------------------------------------------------------- #
# 结论校验
# --------------------------------------------------------------------------- #


def test_conclusion_must_cite_existing_evidence_and_pass_the_check():
    registry, _ = make_registry()
    rejections = []

    def keep(reply):
        def step(messages):
            rejections.append(last_observation(messages))
            return reply

        return step

    model = ScriptedModel(
        [
            tool("lookup", {"term": "Mission"}),
            submit("使命", ["E9"]),
            keep(submit("使命", [])),
            keep(submit("", ["E1"])),
            keep(submit("使命", ["E1"])),
        ]
    )

    def non_empty(conclusion, evidence):
        return None if conclusion else "译名不能为空"

    outcome = run(AgentRunner(model, registry, instructions="x", check_conclusion=non_empty))
    assert outcome.status == "submitted" and outcome.steps == 5
    assert "不存在的证据 E9" in rejections[0]
    assert "没有引用任何证据" in rejections[1]
    assert "译名不能为空" in rejections[2]


# --------------------------------------------------------------------------- #
# 上下文：分页、压缩、上界、条目隔离
# --------------------------------------------------------------------------- #


def test_long_tool_output_is_paged():
    registry, _ = make_registry(output_size=4000)
    model = ScriptedModel(
        [
            tool("lookup", {"term": "Mission"}),
            tool("read_page", {"evidence_id": "E1", "page": 2}),
            submit("使命", ["E1"]),
        ]
    )
    outcome = run(AgentRunner(model, registry, instructions="x", page_size=1500))
    first, second = last_observation(model.calls[1]), last_observation(model.calls[2])
    assert "[E1 第 1/3 页]" in first and '"page": 2' in first and len(first) < 1700
    assert "[E1 第 2/3 页]" in second
    assert outcome.status == "submitted" and outcome.tool_calls == 2


def test_processed_observations_collapse_to_one_line_note():
    registry, _ = make_registry(output_size=3000)
    model = ScriptedModel(
        [
            tool("lookup", {"term": "Mission"}),
            tool("lookup", {"term": "Gates"}, note="Mission 多指宗教使命"),
            submit("使命", ["E1", "E2"], note="Gates 是人名"),
        ]
    )
    run(AgentRunner(model, registry, instructions="x"))
    third_prompt = model.calls[2]
    assert any(m["content"] == "（已处理）[E1] Mission 多指宗教使命" for m in third_prompt)
    assert sum("x" * 1000 in m["content"] for m in third_prompt) == 1  # 只有最近一条观察是完整的


def test_context_length_stays_bounded_over_many_steps():
    registry, _ = make_registry(output_size=50_000)
    steps = 15
    model = ScriptedModel([tool("lookup", {"term": f"t{i}"}, note=f"t{i - 1} 无关") for i in range(steps)], tokens=1)
    tracer = MemoryTracer()
    runner = AgentRunner(
        model, registry, instructions="x", page_size=1000, tracer=tracer, budget=AgentBudget(steps, 10**9, steps)
    )
    outcome = run(runner)
    sizes = [sum(len(m["content"]) for m in call) for call in model.calls]
    fixed = sizes[0]
    per_step = 200 + 160 + 40  # 压缩后每步：动作摘要 + 一行结论 + 包装
    bound = fixed + steps * per_step + 2000 + 1000 + 400  # + 最近一步的完整回复与一页观察
    assert outcome.reason == "budget:steps"
    assert max(sizes) <= bound
    assert max(sizes) < 50_000  # 一次工具输出就有 5 万字符
    assert sizes[-1] - sizes[3] <= (steps - 4) * per_step  # 线性增长的只有每步一行


def test_items_have_isolated_context_and_see_decided_summary():
    registry, _ = make_registry()
    model = ScriptedModel(
        [
            tool("lookup", {"term": "Mission"}),
            submit({"zh": "使命"}, ["E1"]),
            tool("lookup", {"term": "Gates"}),
            submit({"zh": "盖茨"}, ["E1"]),
        ]
    )
    runner = AgentRunner(model, registry, instructions="x")
    outcomes = asyncio.run(runner.run_items([AgentItem("mission", "Mission"), AgentItem("gates", "Gates")]))
    assert [o.conclusion for o in outcomes] == [{"zh": "使命"}, {"zh": "盖茨"}]
    second_item_prompt = "\n".join(m["content"] for m in model.calls[2])
    assert "mission: " in second_item_prompt and "使命" in second_item_prompt  # 已决定事项摘要
    assert "Mission 出现 3 次" not in second_item_prompt  # 上一个条目的观察不带过来
    assert "E1" in [e["id"] for e in outcomes[1].evidence]  # 证据编号按条目从 E1 重新开始


# --------------------------------------------------------------------------- #
# 恢复、追踪回放、LangGraph 节点、LangChain 适配
# --------------------------------------------------------------------------- #


def test_rerun_after_interruption_skips_completed_items(tmp_path):
    items = [AgentItem("a", "A"), AgentItem("b", "B"), AgentItem("c", "C")]
    checkpoint = Checkpoint(tmp_path / "agent.jsonl")
    registry, calls = make_registry()

    def crash(_messages):
        raise ConnectionError("网络中断")

    first = ScriptedModel([tool("lookup", {"term": "A"}), submit("甲", ["E1"]), crash])
    outcomes = asyncio.run(AgentRunner(first, registry, instructions="x").run_items(items, checkpoint))
    assert [o.status for o in outcomes] == ["submitted", "handoff", "handoff"]
    assert list(checkpoint.load()) == ["a"]  # 模型出错转人工的条目不落盘，重跑时再试

    second = ScriptedModel(
        [tool("lookup", {"term": "B"}), submit("乙", ["E1"]), tool("lookup", {"term": "C"}), submit("丙", ["E1"])]
    )
    outcomes = asyncio.run(AgentRunner(second, registry, instructions="x").run_items(items, checkpoint))
    assert [o.conclusion for o in outcomes] == ["甲", "乙", "丙"]
    assert calls == ["A", "B", "C"]  # a 没有重跑
    assert len(second.calls) == 4 and not second.replies
    assert list(checkpoint.load()) == ["a", "b", "c"]


def test_trace_replays_a_complete_run(tmp_path):
    registry, _ = make_registry(output_size=2500)
    tracer = JsonlTracer(tmp_path / "trace.jsonl")
    script = [
        tool("lookup", {"term": "Mission"}),
        tool("read_page", {"evidence_id": "E1", "page": 2}, note="第一页无关"),
        "不是 JSON",
        submit("使命", ["E1"]),
        tool("broken", {"term": "Gates"}),
        tool("lookup", {"term": "Gates"}),
        tool("lookup", {"term": "Gates"}),
        handoff("证据不足"),
    ]
    runner = AgentRunner(ScriptedModel(script), registry, instructions="你是术语 agent", page_size=1000, tracer=tracer)
    original = asyncio.run(runner.run_items([AgentItem("mission", "Mission"), AgentItem("gates", "Gates")]))

    events = tracer.events()
    assert {e["type"] for e in events} >= {"start", "model", "tool", "outcome"}
    assert all("messages" in e and "tokens" in e for e in events if e["type"] == "model")

    def explode(**_):
        raise AssertionError("回放不应执行真实工具")

    offline = ToolRegistry()
    offline.register("lookup", "查", TERM_SCHEMA, explode)
    offline.register("broken", "坏", TERM_SCHEMA, explode)
    replay_runner = AgentRunner(ScriptedModel([]), offline, instructions="你是术语 agent", page_size=1000)
    replayed = asyncio.run(replay(events, replay_runner))
    assert [o.to_dict() for o in replayed] == [o.to_dict() for o in original]


def test_agent_runs_as_a_langgraph_node(tmp_path):
    from langgraph.graph import END, START, StateGraph

    class State(TypedDict, total=False):
        agent_items: list[dict]
        agent_outcomes: list[dict]

    registry, _ = make_registry()
    runner = AgentRunner(
        ScriptedModel([tool("lookup", {"term": "Mission"}), submit("使命", ["E1"])]), registry, instructions="x"
    )
    graph = StateGraph(State)
    graph.add_node("terminology", as_graph_node(runner, checkpoint=Checkpoint(tmp_path / "cp.jsonl")))
    graph.add_edge(START, "terminology")
    graph.add_edge("terminology", END)
    final = asyncio.run(graph.compile().ainvoke({"agent_items": [{"item_id": "m", "task": "Mission"}]}))
    assert final["agent_outcomes"][0]["status"] == "submitted"


def test_langchain_chat_model_adapter_reports_tokens_and_feeds_callbacks():
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from src.infrastructure.observability import UsageMetadataCollector

    reply = AIMessage(
        content=submit("使命", ["E1"]),
        usage_metadata={"input_tokens": 30, "output_tokens": 12, "total_tokens": 42},
    )
    collector = UsageMetadataCollector()
    model = RunnableChatModel(GenericFakeChatModel(messages=iter([reply])), callbacks=[collector])
    result = asyncio.run(model([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]))
    assert isinstance(result, ModelReply) and result.tokens == 42 and '"submit"' in result.text
    assert collector.snapshot.call_count == 1


class RateLimited(Exception):
    status_code = 429


class FlakyRunnable:
    """前几次抛限流错误，之后返回固定回复。"""

    def __init__(self, failures, reply):
        self.failures = failures
        self.reply = reply
        self.calls = 0

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise RateLimited("Error code: 429 - 请求过于频繁")
        return self.reply


@pytest.fixture
def no_wait(monkeypatch):
    async def instant(exc, attempt):
        return None

    monkeypatch.setattr("src.infrastructure.agent.model.wait_before_retry", instant)


def test_chat_model_adapter_retries_transient_errors(no_wait):
    runnable = FlakyRunnable(2, submit("使命", ["E1"]))
    result = asyncio.run(RunnableChatModel(runnable, max_retries=3)([{"role": "user", "content": "u"}]))
    assert runnable.calls == 3 and '"submit"' in result.text


def test_chat_model_adapter_gives_up_after_max_retries(no_wait):
    runnable = FlakyRunnable(5, submit("使命", ["E1"]))
    with pytest.raises(RateLimited):
        asyncio.run(RunnableChatModel(runnable, max_retries=2)([{"role": "user", "content": "u"}]))
    assert runnable.calls == 3


class BrokenModel:
    async def __call__(self, messages, *, metadata=None):
        raise RuntimeError("服务不可用")


def test_model_failure_hands_off_the_item_instead_of_aborting_the_run():
    registry, _ = make_registry()
    runner = AgentRunner(BrokenModel(), registry, instructions="定译名")
    outcomes = asyncio.run(runner.run_items([AgentItem("a", "Mission"), AgentItem("b", "Axis")]))
    assert [o.status for o in outcomes] == ["handoff", "handoff"]
    assert all(o.reason.startswith("model_error") for o in outcomes)
