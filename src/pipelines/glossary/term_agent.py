"""
术语 agent（#18）：翻译前对术语账本里有歧义的候选逐条查证，再决定译名。

一次性的账本调用只凭几处上下文就给全书定一个译名，遇到"既是普通词又是专名"的词容易定错
（Ccru 实测：Mission 定成"使命"，实为海盗 Captain Mission 的名字）。术语 agent 在手写运行框架
（src/infrastructure/agent，ADR-0005）上逐条运行：查出现处、查已有译法、查术语表与账本、
按关键词或意思找定义，然后提交结构化结论或转交人工。

- 只处理被确定性信号标为有歧义的候选（#16），其余候选仍走一次性账本调用；
- 用户术语表始终优先：已在术语表或账本里的词不会被 agent 覆盖；
- 多义项且译名不同的词不并入术语表，只记录供审阅（不强行全书统一）；
- 默认关闭（multi_agent.term_agent.enabled）。开启后 token、调用次数、耗时单独统计。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from ...domain.index.book_index import BookIndex
from ...domain.rules.term_candidates import TermCandidate
from ...domain.rules.term_sources import AMBIGUITY_LABELS
from ...domain.terminology.term_decisions import (
    HANDOFF,
    TermDecision,
    check_conclusion,
    decision_from_outcome,
    glossary_updates,
    summarize,
)
from ...infrastructure.agent import AgentBudget, AgentItem, AgentRunner, ChatModel, Checkpoint, ToolRegistry

logger = logging.getLogger(__name__)

# (term, rendering, label)：其他书的参考译名；(query, limit) → [(position, source, translation)]：按意思找段
Suggestion = tuple[str, str, str]
MeaningSearch = Callable[[str, int], Sequence[tuple[int, str, str | None]]]
# 组装工具注册表：默认用共享工具注册表里的术语 agent 工具（src/application/tools/term_agent.py）
RegistryFactory = Callable[..., ToolRegistry]


def default_registry(**dependencies) -> ToolRegistry:
    from ...application.tools.term_agent import term_agent_registry

    return term_agent_registry(**dependencies)


INSTRUCTIONS = """你是书籍翻译项目的术语查证员。给你一个从整本书里抽出、被标为"可能有歧义"的英文词，
你要查证它在本书里的用法，然后决定它在中文译文里该怎么译。

判断步骤：
1. 用 find_occurrences 看它在全书的出现处（可翻页），注意大小写、前后搭配、所在章节；
2. 用 lookup_glossary 查用户术语表和账本里是否已有相关条目（如全名、同姓的人名），必须与之一致；
3. 需要时用 search_segments 找搭配或定义（如 "Captain Mission"），或用 rendering_distribution 看已有译法；
4. 下结论：
   - 是本书的专名或概念：decision 为 "term"，给出义项。一个词在不同段里意思不同（人名 vs 普通词）就拆成多个义项，
     每个义项写译名 rendering、词义说明 sense、适用范围 scope（book / author / domain，拿不准用 book）、
     证据段号 positions（工具输出里的 [段 N] 的 N）；
   - 只是普通英语词、按上下文翻译即可：decision 为 "not_term"，positions 给出能说明这一点的段号；
   - 证据互相矛盾、或看完仍拿不准：转交人工，写明原因。不要猜。

conclusion 格式：
{"decision": "term", "senses": [{"rendering": "米申", "sense": "人名：海盗 Captain Mission", "scope": "book", "positions": [12, 40]}], "note": "一句话理由"}
{"decision": "not_term", "positions": [3, 8], "note": "一句话理由"}

译名规则：人名地名按通行音译只给中文；自造概念、组织、作品名用中文名词；首字母缩写通常保留原文。"""


@dataclass(frozen=True)
class TermAgentSettings:
    enabled: bool = False
    max_items: int = 40
    budget: AgentBudget = field(default_factory=lambda: AgentBudget(max_steps=8, max_tokens=16_000, max_tool_calls=6))

    @classmethod
    def from_config(cls, config) -> TermAgentSettings:
        getter = getattr(config, "get", None) or (lambda _key, default=None: default)
        defaults = cls()
        return cls(
            enabled=bool(getter("multi_agent.term_agent.enabled", False)),
            max_items=int(getter("multi_agent.term_agent.max_items", defaults.max_items)),
            budget=AgentBudget(
                max_steps=int(getter("multi_agent.term_agent.max_steps", defaults.budget.max_steps)),
                max_tokens=int(getter("multi_agent.term_agent.max_tokens", defaults.budget.max_tokens)),
                max_tool_calls=int(getter("multi_agent.term_agent.max_tool_calls", defaults.budget.max_tool_calls)),
            ),
        )


@dataclass
class TermAgentReport:
    decisions: list[TermDecision] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # 超出 max_items、未运行的候选
    seconds: float = 0.0

    @property
    def handoffs(self) -> list[TermDecision]:
        return [decision for decision in self.decisions if decision.status == HANDOFF]

    def usage(self) -> dict:
        # 每一步是一次模型调用
        return {**summarize(self.decisions), "model_calls": sum(d.steps for d in self.decisions),
                "skipped": len(self.skipped), "seconds": round(self.seconds, 2)}  # fmt: skip

    def updates(self, *protected: Mapping[str, str]) -> dict[str, str]:
        return glossary_updates(self.decisions, *protected)

    def to_dict(self) -> dict:
        return {
            "decisions": [decision.to_dict() for decision in self.decisions],
            "skipped": list(self.skipped),
            "seconds": round(self.seconds, 2),
            "usage": self.usage(),
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> TermAgentReport:
        return cls(
            [TermDecision.from_dict(item) for item in data.get("decisions", [])],
            list(data.get("skipped", [])),
            float(data.get("seconds", 0.0)),
        )


def candidate_task(candidate: TermCandidate) -> str:
    signals = "、".join(AMBIGUITY_LABELS.get(signal, signal) for signal in candidate.ambiguity) or "无"
    contexts = candidate.contexts or [candidate.context]
    lines = [
        f"待查证的词：{candidate.term}（全书 {candidate.count} 次）",
        f"歧义信号：{signals}",
        "抽取时看到的几处上下文（只作线索，结论里的证据要用工具查到的段号）：",
        *(f"  {i}. {context}" for i, context in enumerate(contexts, 1)),
    ]
    return "\n".join(lines)


class TermAgent:
    def __init__(
        self,
        model: ChatModel,
        settings: TermAgentSettings | None = None,
        *,
        tracer=None,
        checkpoint: Checkpoint | None = None,
        registry_factory: RegistryFactory = default_registry,
    ):
        self.model = model
        self.registry_factory = registry_factory
        self.settings = settings or TermAgentSettings(enabled=True)
        self.tracer = tracer
        self.checkpoint = checkpoint

    def runner(self, registry: ToolRegistry) -> AgentRunner:
        return AgentRunner(
            self.model,
            registry,
            instructions=INSTRUCTIONS,
            name="term-agent",
            budget=self.settings.budget,
            check_conclusion=check_conclusion,
            tracer=self.tracer,
        )

    async def run(
        self,
        candidates: Sequence[TermCandidate],
        index: BookIndex,
        glossary: Mapping[str, str],
        ledger: Mapping[str, str] | None = None,
        *,
        suggestions: Sequence[Suggestion] = (),
        search_by_meaning: MeaningSearch | None = None,
    ) -> TermAgentReport:
        """逐条查证；已在用户术语表或账本里的候选不运行。只有模型调用本身的异常会向上抛。"""
        ledger = ledger or {}
        known = {term.casefold() for term in [*glossary, *ledger]}
        pending = [candidate for candidate in candidates if candidate.term.casefold() not in known]
        selected, skipped = pending[: self.settings.max_items], pending[self.settings.max_items :]
        registry = self.registry_factory(
            index=index,
            glossary=glossary,
            ledger=ledger,
            suggestions=suggestions,
            search_by_meaning=search_by_meaning,
        )
        items = [AgentItem(candidate.term, candidate_task(candidate)) for candidate in selected]
        started = time.monotonic()
        outcomes = await self.runner(registry).run_items(items, self.checkpoint)
        decisions = []
        for candidate, item, outcome in zip(selected, items, outcomes, strict=True):
            decision = decision_from_outcome(item.item_id, outcome.to_dict())
            if decision.status == HANDOFF:
                decision.contexts = list(candidate.contexts or [candidate.context])
            decisions.append(decision)
        report = TermAgentReport(decisions, [candidate.term for candidate in skipped], time.monotonic() - started)
        logger.info("术语 agent：%s", report.usage())
        return report


__all__ = ["INSTRUCTIONS", "TermAgent", "TermAgentReport", "TermAgentSettings", "candidate_task"]
