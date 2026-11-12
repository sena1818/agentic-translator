"""
术语 agent 的决定：结论格式、证据校验、并入术语表的规则。纯逻辑，不读写文件、不调用模型。

agent 对一个有歧义的候选只有三种结局：
- decided：给出一个或多个义项，每个义项有译名、词义说明、适用范围和证据段；
- not_term：只是普通词，按上下文翻译，不进术语表；
- handoff：拿不准、或预算用尽，转交人工。

证据可追溯：每个义项引用的段号必须出现在 agent 实际查到的工具输出里（工具输出用"[段 N]"标出段号）。
并入术语表时用户术语表始终优先；有多个不同译名的词不作为硬约束并入（统一成一个译名正是 Mission 被
误译成"使命"的原因），只记录供人工和提示参考。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field

from ..memory.corrections import SCOPES

DECIDED = "decided"
NOT_TERM = "not_term"
HANDOFF = "handoff"

SEGMENT_MARK = "段"
_POSITION_RE = re.compile(r"\[段 (\d+)\]")


def segment_marker(position: int) -> str:
    """工具输出里标注段号的写法；结论引用的段号据此核对。"""
    return f"[{SEGMENT_MARK} {position}]"


def positions_in(text: str) -> set[int]:
    return {int(match) for match in _POSITION_RE.findall(text)}


@dataclass(frozen=True)
class SenseDecision:
    rendering: str
    sense: str
    scope: str = "book"
    positions: tuple[int, ...] = ()


@dataclass
class TermDecision:
    term: str
    status: str  # decided | not_term | handoff
    senses: list[SenseDecision] = field(default_factory=list)
    positions: tuple[int, ...] = ()  # not_term 的证据段
    note: str = ""
    reason: str = ""  # handoff 原因：agent:… / budget:… / loop:…
    evidence: list[dict] = field(default_factory=list)  # 引用的证据（工具名、参数、摘录）
    contexts: list[str] = field(default_factory=list)  # 抽取时的上下文，转交人工时供审阅
    steps: int = 0
    tokens: int = 0
    tool_calls: int = 0

    @property
    def renderings(self) -> list[str]:
        return list(dict.fromkeys(sense.rendering for sense in self.senses))

    @property
    def single_rendering(self) -> str | None:
        """只有一个译名时返回它；多义项且译名不同的词不作为全书统一译名。"""
        renderings = self.renderings
        return renderings[0] if self.status == DECIDED and len(renderings) == 1 else None

    @property
    def evidence_positions(self) -> list[int]:
        found = [p for sense in self.senses for p in sense.positions] + list(self.positions)
        return sorted(set(found))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> TermDecision:
        senses = [
            SenseDecision(**{**sense, "positions": tuple(sense.get("positions", ()))}) for sense in data["senses"]
        ]
        fields = {key: data[key] for key in cls.__dataclass_fields__ if key in data and key != "senses"}
        fields["positions"] = tuple(fields.get("positions", ()))
        return cls(senses=senses, **fields)


def check_conclusion(conclusion, evidence: Mapping[str, dict]) -> str | None:
    """agent 结论的业务校验；返回 None 表示通过，否则返回给模型看的原因。

    evidence 是本条目已查到的全部证据 {编号: {tool, args, output}}，引用的段号必须在其中出现过。
    """
    if not isinstance(conclusion, dict):
        return "conclusion 必须是 JSON 对象"
    seen = set().union(*(positions_in(str(item.get("output", ""))) for item in evidence.values()))
    decision = conclusion.get("decision")
    if decision == NOT_TERM:
        positions = conclusion.get("positions")
        if not _int_list(positions) or not positions:
            return "decision=not_term 时要用 positions 给出能说明它只是普通词的段号"
        missing = sorted(set(positions) - seen)
        return f"段号 {missing} 没有出现在已查到的证据里" if missing else None
    if decision != "term":
        return 'decision 只能是 "term" 或 "not_term"'
    senses = conclusion.get("senses")
    if not isinstance(senses, list) or not senses:
        return "decision=term 时 senses 至少要有一个义项"
    for i, sense in enumerate(senses, 1):
        if not isinstance(sense, dict):
            return f"第 {i} 个义项必须是 JSON 对象"
        if not str(sense.get("rendering", "")).strip():
            return f"第 {i} 个义项缺少 rendering（译名）"
        if not str(sense.get("sense", "")).strip():
            return f"第 {i} 个义项缺少 sense（词义说明）"
        if sense.get("scope", "book") not in SCOPES:
            return f"第 {i} 个义项的 scope 只能是 {', '.join(SCOPES)}"
        positions = sense.get("positions")
        if not _int_list(positions) or not positions:
            return f"第 {i} 个义项要用 positions 给出证据段号（工具输出里的 [段 N]）"
        missing = sorted(set(positions) - seen)
        if missing:
            return f"第 {i} 个义项引用的段号 {missing} 没有出现在已查到的证据里"
    return None


def _int_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, int) and not isinstance(item, bool) for item in value)


def decision_from_outcome(term: str, outcome: Mapping) -> TermDecision:
    """把运行框架的结局（AgentOutcome.to_dict()）转成术语决定。"""
    usage = {key: int(outcome.get(key, 0)) for key in ("steps", "tokens", "tool_calls")}
    evidence = list(outcome.get("evidence") or [])
    if outcome.get("status") != "submitted":
        return TermDecision(term, HANDOFF, reason=str(outcome.get("reason", "")), evidence=evidence, **usage)
    conclusion = outcome.get("conclusion") or {}
    note = str(conclusion.get("note", ""))
    if conclusion.get("decision") == NOT_TERM:
        positions = tuple(conclusion.get("positions") or ())
        return TermDecision(term, NOT_TERM, positions=positions, note=note, evidence=evidence, **usage)
    senses = [
        SenseDecision(
            str(sense["rendering"]).strip(),
            str(sense["sense"]).strip(),
            sense.get("scope", "book"),
            tuple(sense.get("positions") or ()),
        )
        for sense in conclusion.get("senses", [])
    ]
    return TermDecision(term, DECIDED, senses=senses, note=note, evidence=evidence, **usage)


def glossary_updates(decisions: Iterable[TermDecision], *protected: Mapping[str, str]) -> dict[str, str]:
    """可以并入本次术语表的统一译名。

    - 只收单一译名的 decided；多义项、普通词、转交人工的都不并入；
    - protected 里已有的术语（用户术语表、账本，大小写不敏感）一律不覆盖。
    """
    taken = {term.casefold() for mapping in protected for term in mapping}
    updates: dict[str, str] = {}
    for decision in decisions:
        rendering = decision.single_rendering
        if rendering and decision.term.casefold() not in taken:
            updates[decision.term] = rendering
            taken.add(decision.term.casefold())
    return updates


def summarize(decisions: Iterable[TermDecision]) -> dict:
    decisions = list(decisions)
    count = len(decisions)
    by_status = {status: sum(1 for d in decisions if d.status == status) for status in (DECIDED, NOT_TERM, HANDOFF)}
    return {
        "items": count,
        **by_status,
        "multi_sense": sum(1 for d in decisions if d.status == DECIDED and len(d.renderings) > 1),
        "steps": sum(d.steps for d in decisions),
        "tokens": sum(d.tokens for d in decisions),
        "tool_calls": sum(d.tool_calls for d in decisions),
        "mean_steps": round(sum(d.steps for d in decisions) / count, 2) if count else 0.0,
        "handoff_rate": round(by_status[HANDOFF] / count, 4) if count else 0.0,
    }


__all__ = [
    "DECIDED",
    "HANDOFF",
    "NOT_TERM",
    "SenseDecision",
    "TermDecision",
    "check_conclusion",
    "decision_from_outcome",
    "glossary_updates",
    "positions_in",
    "segment_marker",
    "summarize",
]
