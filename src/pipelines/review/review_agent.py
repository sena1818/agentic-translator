"""
审校 agent：对翻译后仍被标记的问题逐条分诊——能修的修、误报的驳回、拿不准的转人工。

建在手写 agent 运行框架（src/infrastructure/agent，ADR-0005）上：
- 每条问题是一个条目，独立上下文，只带"前面已决定事项"的摘要；
- 预算：每条问题的步数、token、工具调用次数都有上限，超出即转人工；
- 结论校验：提交修改时在框架里重新应用编辑、重新质检，没通过"逐类型不退步"的修改被拒绝，
  原因作为观察返回，agent 可以改了再交或转人工——所以生效的修改一定通过了检查；
- 编辑修不了的问题（缺段、结构、图片/公式/代码/链接对不上）不进 agent，直接转人工，不花 token。

默认关闭（review_agent.enabled: false），关闭时流程与现状一致。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ...domain.review.triage import (
    DISMISSED,
    FIXED,
    HANDOFF,
    FlaggedIssue,
    TriageResult,
    check_dismiss_reason,
    needs_direct_handoff,
    summarize,
)
from ...infrastructure.agent import AgentBudget, AgentItem, AgentRunner, ChatModel, Checkpoint, ToolRegistry
from ...infrastructure.agent.runner import MODEL_ERROR
from ...infrastructure.agent.trace import Tracer
from .session import ReviewSession
from .workspace import ReviewWorkspace

INSTRUCTIONS = """你是严谨的中英翻译审校员。每次只处理一条被质检标记的问题，任务是分诊：
- 确实是错误、而且能用局部编辑修好 → 提交修改；
- 质检误报（译文其实没问题，比如术语表的词在这里是普通词义、中文换了合理的说法）→ 驳回并写明理由；
- 拿不准，或需要整段重译、改结构、查原书 → 转交人工。

做法：先用 get_issue 看问题和该段原文、译文；需要时查前后文、术语表、全书译法与出现处、数字/链接等对象。
要修改时先用 check_fix 试编辑，确认 accepted 为 true 再提交。编辑只能把译文里恰好出现一次的
一段文字换成另一段，改动片段不超过 40 字；不能整段重写，也不能碰 <...> 标签。

提交结论的格式（conclusion）：
- 修改：{"verdict": "fix", "edits": [{"from": "译文中的原文字", "to": "新文字"}]}
- 驳回：{"verdict": "dismiss", "reason": "为什么是误报"}
不要为了让问题消失而改动原意；没有把握就转交人工。"""


@dataclass(frozen=True)
class ReviewAgentConfig:
    enabled: bool = False
    max_issues: int = 200  # 一次分诊最多交给 agent 的问题数；其余留待下次或人工
    budget: AgentBudget = field(default_factory=lambda: AgentBudget(max_steps=8, max_tokens=12_000, max_tool_calls=6))

    @classmethod
    def from_config(cls, config) -> ReviewAgentConfig:
        get = getattr(config, "get", None) or (lambda _key, default=None: default)
        return cls(
            enabled=bool(get("review_agent.enabled", False)),
            max_issues=int(get("review_agent.max_issues", 200)),
            budget=AgentBudget(
                max_steps=int(get("review_agent.max_steps", 8)),
                max_tokens=int(get("review_agent.max_tokens", 12_000)),
                max_tool_calls=int(get("review_agent.max_tool_calls", 6)),
            ),
        )


@dataclass
class TriageRun:
    results: list[TriageResult]
    seconds: float = 0.0

    @property
    def summary(self) -> dict:
        return {**summarize(self.results), "seconds": round(self.seconds, 3)}


def _task_text(issue: FlaggedIssue, task_id: str | None = None) -> str:
    text = (
        f"待分诊问题 {issue.issue_id}：第 {issue.seg_id} 段，类型 {issue.kind}，严重度 {issue.severity}。\n"
        f"描述：{issue.message}\n先调用 get_issue 查看原文与译文。"
    )
    if task_id:
        text += f"\n查全书的工具需要 task_id：{task_id}；read_segment 的 segment 即段号。"
    return text


def _decided(results: Sequence[TriageResult], limit: int = 12) -> str:
    """前面已决定事项的摘要（只取已修与已驳回，各一行），让后面的判断保持一致。"""
    lines = []
    for result in [r for r in results if r.verdict in {FIXED, DISMISSED} and r.by == "agent"][-limit:]:
        detail = (
            "；".join(f"{e.old[:12]}→{e.new[:12]}" for e in result.edits)
            if result.verdict == FIXED
            else result.reason[:40]
        )
        lines.append(f"- 段 {result.issue.seg_id} {result.issue.kind}：{result.verdict}（{detail}）")
    return "\n".join(lines)


RegistryFactory = Callable[[ReviewSession], ToolRegistry]


class ReviewAgent:
    """registry_factory 由会话生成工具注册表：工具定义在共享工具注册表里
    （src/application/tools/triage.py 的 review_registry），本层不依赖它的具体实现。"""

    def __init__(
        self,
        model: ChatModel,
        registry_factory: RegistryFactory,
        config: ReviewAgentConfig | None = None,
        tracer: Tracer | None = None,
    ):
        self.model = model
        self.registry_factory = registry_factory
        self.config = config or ReviewAgentConfig(enabled=True)
        self.tracer = tracer

    def _runner(self, session: ReviewSession) -> AgentRunner:
        def check_conclusion(conclusion: Any, _evidence: dict) -> str | None:
            if not isinstance(conclusion, dict) or conclusion.get("verdict") not in {"fix", "dismiss"}:
                return 'conclusion 必须是 {"verdict": "fix", "edits": [...]} 或 {"verdict": "dismiss", "reason": "..."}'
            if conclusion["verdict"] == "dismiss":
                return check_dismiss_reason(conclusion.get("reason")) or None
            result = session.try_edits(conclusion.get("edits"))
            if not result["accepted"]:
                return "修改没有通过逐类型不退步检查：" + result["reason"] + "。可以换一种改法，或转交人工"
            return None

        return AgentRunner(
            self.model,
            self.registry_factory(session),
            instructions=INSTRUCTIONS,
            name="review",
            budget=self.config.budget,
            check_conclusion=check_conclusion,
            tracer=self.tracer,
        )

    async def triage(
        self,
        workspace: ReviewWorkspace,
        issues: Sequence[FlaggedIssue] | None = None,
        checkpoint: Checkpoint | None = None,
        task_id: str | None = None,
    ) -> TriageRun:
        """逐条分诊；已修的段立即更新到工作区，后面同段的问题看到的是新译文。"""
        started = time.monotonic()
        session = ReviewSession(workspace)
        runner = self._runner(session)
        done = {key: TriageResult.from_dict(value) for key, value in (checkpoint.load() if checkpoint else {}).items()}
        results: list[TriageResult] = []
        sent = 0
        for issue in workspace.flagged if issues is None else issues:
            if issue.issue_id in done:
                result = done[issue.issue_id]
                if result.verdict == FIXED:
                    workspace.set_translation(issue.seg_id, result.after)
                results.append(result)
                continue
            current = workspace.translation(issue.seg_id)
            reason = needs_direct_handoff(issue)
            if reason:
                result = TriageResult(issue, HANDOFF, reason, before=current, by="rule")
            elif sent >= self.config.max_issues:
                result = TriageResult(issue, HANDOFF, "超出本次分诊条数上限，未处理", before=current, by="limit")
            else:
                sent += 1
                result = await self._run_one(runner, session, issue, current, results, task_id)
            if checkpoint is not None and not result.reason.startswith(MODEL_ERROR):
                checkpoint.save({"item_id": issue.issue_id, **result.to_dict()})
            results.append(result)
        return TriageRun(results, time.monotonic() - started)

    async def _run_one(
        self, runner: AgentRunner, session: ReviewSession, issue: FlaggedIssue, current: str, results, task_id
    ) -> TriageResult:
        session.current = issue
        outcome = await runner.run(AgentItem(issue.issue_id, _task_text(issue, task_id)), _decided(results))
        cost = {"steps": outcome.steps, "tokens": outcome.tokens, "tool_calls": outcome.tool_calls}
        if outcome.status != "submitted":
            return TriageResult(issue, HANDOFF, outcome.reason, before=current, **cost)
        conclusion = outcome.conclusion
        if conclusion.get("verdict") == "dismiss":
            return TriageResult(issue, DISMISSED, str(conclusion["reason"]).strip(), before=current, **cost)
        checked = session.try_edits(conclusion.get("edits"))
        if not checked["accepted"]:  # 结论校验已拦过；这里再确认一次，绝不让没通过检查的修改生效
            return TriageResult(issue, HANDOFF, "修改未通过检查：" + checked["reason"], before=current, **cost)
        session.workspace.set_translation(issue.seg_id, checked["translation"])
        return TriageResult(issue, FIXED, "", checked["edits"], before=current, after=checked["translation"], **cost)


def untriaged(workspace: ReviewWorkspace, issues: Sequence[FlaggedIssue] | None = None) -> TriageRun:
    """关闭审校 agent 时的结局：所有被标记的问题原样留给人工，不调用模型（即现状）。"""
    pending = workspace.flagged if issues is None else issues
    return TriageRun(
        [
            TriageResult(issue, HANDOFF, "未启用审校 agent", before=workspace.translation(issue.seg_id), by="disabled")
            for issue in pending
        ]
    )


def results_to_json(run: TriageRun) -> str:
    return json.dumps(
        {"summary": run.summary, "results": [r.to_dict() for r in run.results]}, ensure_ascii=False, indent=2
    )
