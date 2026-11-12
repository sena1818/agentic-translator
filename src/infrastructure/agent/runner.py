"""
手写的 agent 运行框架（ADR-0005）。

一个条目（一个待查证的术语、一个被标记的问题）的运行：

    模型回复 → 解析动作 → 调用工具 / 校验结论 / 转交人工 → 观察结果写回上下文 → 下一步

结束方式只有三种：提交结论（submitted）、模型主动转交人工（handoff）、
框架强制转交人工（handoff，原因为预算用尽或重复调用）。所有失败都降级为转交人工，不抛异常；
只有模型调用本身的异常（网络、额度）会向上抛，由调用方决定重试，已完成的条目不受影响。

护栏：
- 预算：步数、token、工具调用次数三重上限，任一超出即转交人工并写明哪一项。
- 重复调用：同一工具、同一参数第二次出现时不执行，注入提示；提示后再重复则终止。
- 结论校验：引用的证据编号必须存在；可再挂一个业务校验函数，失败时把原因作为观察返回。
- 上下文：见 context.ContextWindow；每个条目独立上下文，只带全局已决定事项的摘要。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from .context import ContextWindow, EvidenceStore, action_summary
from .model import ChatModel, ModelReply, ProtocolError, ScriptedModel, estimate_tokens, parse_action
from .tools import ToolRegistry, ToolResult
from .trace import Checkpoint, Tracer

READ_PAGE = "read_page"
_READ_PAGE_SCHEMA = {
    "type": "object",
    "properties": {"evidence_id": {"type": "string"}, "page": {"type": "integer", "minimum": 1}},
    "required": ["evidence_id", "page"],
    "additionalProperties": False,
}

PROTOCOL = """每一步只输出一个 JSON 对象，不要输出其他内容。三种动作：
1. 调用工具：{"thought": "为什么", "note": "对上一条观察的一行结论", "action": "tool", "tool": "工具名", "args": {...}}
2. 提交结论：{"thought": "...", "note": "...", "action": "submit", "conclusion": {...}, "evidence": ["E1"]}
   evidence 必须引用观察里出现过的证据编号（如 E1、E2），至少一个。
3. 转交人工：{"thought": "...", "action": "handoff", "reason": "拿不准的原因"}
工具输出过长时只给第一页，用 read_page 翻页。不要用相同参数重复调用同一个工具。"""


@dataclass(frozen=True)
class AgentBudget:
    max_steps: int = 12
    max_tokens: int = 24_000
    max_tool_calls: int = 8


@dataclass(frozen=True)
class AgentItem:
    item_id: str
    task: str


@dataclass
class AgentOutcome:
    item_id: str
    status: str  # submitted | handoff
    conclusion: Any = None
    evidence: list[dict] = field(default_factory=list)
    reason: str = ""  # handoff 时的原因：agent:… / budget:steps|tokens|tool_calls / loop:…
    steps: int = 0
    tokens: int = 0
    tool_calls: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> AgentOutcome:
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


ConclusionCheck = Callable[[Any, dict[str, dict]], str | None]
Summarizer = Callable[[Sequence[AgentOutcome]], str]


def summarize_decided(outcomes: Sequence[AgentOutcome], max_lines: int = 20, line_chars: int = 100) -> str:
    """全局已决定事项的摘要：最近 max_lines 个已提交条目各一行，长度有上界。"""
    lines = []
    for outcome in [o for o in outcomes if o.status == "submitted"][-max_lines:]:
        text = json.dumps(outcome.conclusion, ensure_ascii=False, default=str)
        lines.append(f"- {outcome.item_id}: {text[:line_chars]}")
    return "\n".join(lines)


MODEL_ERROR = "model_error"  # 模型调用重试用尽仍失败；这类转人工不落盘，重跑时再试


class AgentRunner:
    def __init__(
        self,
        model: ChatModel,
        registry: ToolRegistry,
        *,
        instructions: str,
        name: str = "agent",
        budget: AgentBudget | None = None,
        page_size: int = 1500,
        require_evidence: bool = True,
        check_conclusion: ConclusionCheck | None = None,
        tracer: Tracer | None = None,
    ):
        if READ_PAGE in registry.tools:
            raise ValueError(f"{READ_PAGE} 是框架内置工具，不能再注册")
        self.model = model
        self.registry = registry
        self.instructions = instructions
        self.name = name
        self.budget = budget or AgentBudget()
        self.page_size = page_size
        self.require_evidence = require_evidence
        self.check_conclusion = check_conclusion
        self.tracer = tracer

    # ------------------------------------------------------------------ #

    def _system_prompt(self, decided: str) -> str:
        tools = self.registry.describe()
        builtin = f"- {READ_PAGE}: 翻看某条证据的其他页\n  参数 schema: {json.dumps(_READ_PAGE_SCHEMA)}"
        parts = [self.instructions, "【可用工具】", f"{tools}\n{builtin}".strip(), "【输出协议】", PROTOCOL]
        if decided:
            parts += ["【已决定事项（供参考，保持一致）】", decided]
        return "\n\n".join(parts)

    def _record(self, item_id: str, step: int, event_type: str, **payload) -> None:
        if self.tracer is not None:
            self.tracer.record({"agent": self.name, "item": item_id, "step": step, "type": event_type, **payload})

    async def run(self, item: AgentItem, decided: str = "") -> AgentOutcome:
        store = EvidenceStore(self.page_size)
        context = ContextWindow(self._system_prompt(decided), item.task)
        seen: dict[str, str] = {}  # 调用签名 → 证据编号
        warned_repeat = False
        steps = tokens = tool_calls = 0
        self._record(item.item_id, 0, "start", task=item.task, decided=decided, budget=asdict(self.budget))

        def finish(status: str, *, conclusion: Any = None, cited: Sequence[str] = (), reason: str = "") -> AgentOutcome:
            evidence = [
                {"id": e.evidence_id, "tool": e.tool, "args": e.args, "excerpt": e.excerpt()}
                for e in (store.items[i] for i in cited)
            ]
            outcome = AgentOutcome(item.item_id, status, conclusion, evidence, reason, steps, tokens, tool_calls)
            self._record(item.item_id, steps, "outcome", outcome=outcome.to_dict())
            return outcome

        while True:
            if steps >= self.budget.max_steps:
                return finish("handoff", reason="budget:steps")
            if tokens >= self.budget.max_tokens:
                return finish("handoff", reason="budget:tokens")

            messages = context.messages()
            try:
                reply = await self.model(
                    messages, metadata={"agent": self.name, "item": item.item_id, "step": steps + 1}
                )
            except Exception as exc:  # 重试用尽仍失败：只放弃这一条，其余条目照常运行
                self._record(item.item_id, steps, "model_error", error=f"{type(exc).__name__}: {exc}")
                return finish("handoff", reason=f"{MODEL_ERROR}: {type(exc).__name__}: {str(exc)[:200]}")
            steps += 1
            used = reply.tokens or (
                estimate_tokens("".join(m["content"] for m in messages)) + estimate_tokens(reply.text)
            )
            tokens += used
            self._record(
                item.item_id,
                steps,
                "model",
                messages=messages,
                reply=reply.text,
                tokens=used,
                context_chars=context.size(),
            )

            try:
                action = parse_action(reply.text)
            except ProtocolError as exc:
                context.add(reply.text, "（不合协议的回复）", f"回复不符合输出协议：{exc}。请只输出一个 JSON 对象。")
                continue
            context.annotate_last(action.note)

            if action.kind == "handoff":
                return finish("handoff", reason=f"agent:{action.reason or '未说明'}")

            if action.kind == "submit":
                problem = self._reject_reason(action.conclusion, action.evidence, store)
                if problem:
                    self._record(item.item_id, steps, "rejected", reason=problem)
                    context.add(reply.text, action_summary("submit"), f"结论被拒绝：{problem}。请修正后重新提交。")
                    continue
                return finish("submitted", conclusion=action.conclusion, cited=action.evidence)

            signature = json.dumps([action.tool, action.args], ensure_ascii=False, sort_keys=True)
            summary = action_summary("tool", tool=action.tool, args=action.args)
            if signature in seen:
                if warned_repeat:
                    return finish("handoff", reason=f"loop:重复调用 {action.tool}")
                warned_repeat = True
                context.add(
                    reply.text,
                    summary,
                    f"你已经用相同参数调用过 {action.tool}，结果见 {seen[signature]}。"
                    "换一种查法，或基于已有证据提交结论 / 转交人工；再次重复将终止。",
                )
                continue
            if tool_calls >= self.budget.max_tool_calls:
                return finish("handoff", reason="budget:tool_calls")
            tool_calls += 1

            if action.tool == READ_PAGE:
                observation, evidence_id = self._read_page(store, action.args)
            else:
                result = await self.registry.call(action.tool, action.args)
                self._record(
                    item.item_id, steps, "tool", tool=action.tool, args=action.args, ok=result.ok, output=result.output
                )
                evidence = store.add(action.tool, action.args, result.output, result.ok)
                evidence_id = evidence.evidence_id
                observation = store.render(evidence_id)
            seen[signature] = evidence_id or "上一步"
            context.add(reply.text, summary, observation, evidence_id)

    def _read_page(self, store: EvidenceStore, args: dict) -> tuple[str, str | None]:
        from .tools import validate_args

        errors = validate_args(_READ_PAGE_SCHEMA, args)
        if errors:
            return "参数校验失败：" + "；".join(errors), None
        evidence_id, page = args["evidence_id"], args["page"]
        if evidence_id not in store.items:
            return f"没有证据 {evidence_id}。已有：{', '.join(store.items) or '无'}", None
        if page > store.pages(evidence_id):
            return f"{evidence_id} 只有 {store.pages(evidence_id)} 页", evidence_id
        return store.render(evidence_id, page), evidence_id

    def _reject_reason(self, conclusion: Any, cited: list[str], store: EvidenceStore) -> str | None:
        unknown = [evidence_id for evidence_id in cited if evidence_id not in store.items]
        if unknown:
            return f"引用了不存在的证据 {', '.join(unknown)}（已有：{', '.join(store.items) or '无'}）"
        if self.require_evidence and not cited:
            return "结论没有引用任何证据编号"
        if conclusion is None:
            return "缺少 conclusion"
        if self.check_conclusion is not None:
            evidence = {i: {"tool": e.tool, "args": e.args, "output": e.output} for i, e in store.items.items()}
            return self.check_conclusion(conclusion, evidence)
        return None

    # ------------------------------------------------------------------ #

    async def run_items(
        self,
        items: Sequence[AgentItem],
        checkpoint: Checkpoint | None = None,
        summarize: Summarizer = summarize_decided,
    ) -> list[AgentOutcome]:
        """逐条目运行；每完成一个就落盘，重新运行时跳过已完成的条目。"""
        done = {key: AgentOutcome.from_dict(value) for key, value in (checkpoint.load() if checkpoint else {}).items()}
        outcomes: list[AgentOutcome] = []
        for item in items:
            if item.item_id in done:
                outcomes.append(done[item.item_id])
                continue
            outcome = await self.run(item, summarize(outcomes))
            if checkpoint is not None and not outcome.reason.startswith(MODEL_ERROR):
                checkpoint.save(outcome.to_dict())
            outcomes.append(outcome)
        return outcomes


# ---------------------------------------------------------------------- #
# 回放
# ---------------------------------------------------------------------- #


class _RecordedTools(ToolRegistry):
    """按追踪里的顺序返回工具输出；名称与参数对不上说明轨迹已偏离。"""

    def __init__(self, registry: ToolRegistry, events: list[dict]):
        super().__init__(dict(registry.tools))
        self.queue = [event for event in events if event["type"] == "tool"]

    async def call(self, name: str, args: Any) -> ToolResult:
        event = self.queue.pop(0)
        if event["tool"] != name or event["args"] != args:
            raise RuntimeError(f"回放偏离：追踪记录的是 {event['tool']}({event['args']})，实际调用 {name}({args})")
        return ToolResult(event["ok"], event["output"])


async def replay(events: list[dict], runner: AgentRunner) -> list[AgentOutcome]:
    """用追踪里记录的模型回复和工具输出重放一次运行，不调用模型、不执行工具。

    runner 提供与原运行相同的配置（说明、预算、工具声明）；返回的结局应与原运行一致。
    """
    outcomes = []
    for item_id in dict.fromkeys(event["item"] for event in events if event["type"] == "start"):
        item_events = [event for event in events if event["item"] == item_id]
        start = next(event for event in item_events if event["type"] == "start")
        replayer = AgentRunner(
            ScriptedModel([ModelReply(e["reply"], e["tokens"]) for e in item_events if e["type"] == "model"]),
            _RecordedTools(runner.registry, item_events),
            instructions=runner.instructions,
            name=runner.name,
            budget=AgentBudget(**start["budget"]),
            page_size=runner.page_size,
            require_evidence=runner.require_evidence,
            check_conclusion=runner.check_conclusion,
        )
        outcomes.append(await replayer.run(AgentItem(item_id, start["task"]), start["decided"]))
    return outcomes


# ---------------------------------------------------------------------- #
# LangGraph 节点
# ---------------------------------------------------------------------- #


def as_graph_node(
    runner: AgentRunner,
    *,
    items_key: str = "agent_items",
    outcomes_key: str = "agent_outcomes",
    checkpoint: Checkpoint | None = None,
):
    """把 agent 包成 LangGraph 节点：从状态取条目列表，写回结局列表。

    外层编排（何时运行、并发、与翻译流程的先后）仍由 LangGraph 负责；
    节点内部的多步推理由本框架负责，图状态里只出现输入条目和最终结局。
    """

    async def node(state: dict) -> dict:
        items = [item if isinstance(item, AgentItem) else AgentItem(**item) for item in state.get(items_key, [])]
        outcomes = await runner.run_items(items, checkpoint)
        return {outcomes_key: [outcome.to_dict() for outcome in outcomes]}

    return node
