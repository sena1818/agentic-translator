"""
审校分诊的纯规则：哪些问题要分诊、哪些直接转人工、修改怎么约束、修改何时生效。

审校 agent 对每条被标记的问题给出三种结局之一：
- fixed      已修：提交的修改通过"逐类型不退步"检查后生效；
- dismissed  已驳回：判为误报，必须写明理由，译文不动；
- handoff    待人工：拿不准、修改没通过检查、预算用尽，或该类问题本就不该由编辑修。

修改只能是约束式编辑（把某段文字换成另一段），不能整段重写：每条编辑的 from 必须在该段译文中
恰好出现一次、不碰标签，被改动的片段有长度上限。编辑由程序应用，其余文字物理上不会被改。

生效条件（与分类型修复同一标准，见 domain/rules/typed_repair.py）：目标问题消失，
且没有新增任何其他高/中严重度的问题。本模块不读写文件、不调用模型。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Protocol

FIXED = "fixed"
DISMISSED = "dismissed"
HANDOFF = "handoff"
VERDICTS = (FIXED, DISMISSED, HANDOFF)
VERDICT_LABELS = {FIXED: "已修", DISMISSED: "已驳回", HANDOFF: "待人工"}

SEVERE = frozenset({"high", "medium"})
# 编辑改不了的问题：缺段、截断、结构标签、图片/公式/代码/表格/链接/脚注对不上。
# 这些要重译或改结构，分类型修复已经试过；不花 token，直接转人工。
_DIRECT_HANDOFF = frozenset({"structure", "missing", "truncated", "empty", "markdown", "repair"})
_DIRECT_HANDOFF_AUDIT = frozenset({"image", "math", "code", "table", "footnote", "link", "image_text"})

MAX_EDITS = 4
MAX_EDIT_SPAN = 40  # 一条编辑里被替换（或插入）的片段最多这么长
MIN_DISMISS_REASON = 6
_TRIAGE_MARKS = ("[待人工]", "[已驳回]", "[已修]")


class IssueLike(Protocol):
    kind: str
    severity: str
    message: str


@dataclass(frozen=True)
class CheckedIssue:
    """重新质检得到的一条问题。"""

    kind: str
    severity: str
    message: str


@dataclass(frozen=True)
class FlaggedIssue:
    """一条待分诊的问题，定位到对照阅读器的段号。"""

    issue_id: str
    seg_id: int
    chunk_index: int
    kind: str
    severity: str
    message: str
    origin: str = "quality"  # quality（规则质检 / 语义提示 / 修复记录）| audit（非文字审计）

    def to_dict(self) -> dict:
        return asdict(self)


def _strip_segment_prefix(message: str) -> str:
    return re.sub(r"^\[段 \d+\]\s*", "", message)


def flagged_from_records(records: Sequence[dict], audit: dict | None = None) -> list[FlaggedIssue]:
    """从块记录的质检报告与非文字审计结果里收集待分诊的问题（只要高/中严重度、能定位到段的）。

    已分诊过的问题（消息带"[待人工]"等标记、或已降为 low）不会重复收集。
    """
    flagged: list[FlaggedIssue] = []
    seen: set[tuple[int, str, str]] = set()
    for record in records:
        report = record.get("quality_report") or {}
        for issue in report.get("issues") or []:
            if issue.get("segment") is None or issue.get("severity") not in SEVERE:
                continue
            message = _strip_segment_prefix(str(issue.get("message", "")))
            if message.startswith(_TRIAGE_MARKS):
                continue
            key = (int(issue["segment"]), str(issue.get("kind")), message)
            if key in seen:
                continue
            seen.add(key)
            flagged.append(
                FlaggedIssue(
                    f"q{len(flagged) + 1}",
                    int(issue["segment"]),
                    int(record.get("chunk_index", -1)),
                    str(issue.get("kind")),
                    str(issue.get("severity")),
                    message,
                )
            )
    chunk_of = _chunk_of_segments(records)
    for finding in (audit or {}).get("findings") or []:
        if finding.get("origin") != "translation" or finding.get("segment") is None:
            continue
        if finding.get("severity") not in SEVERE:
            continue
        seg_id = int(finding["segment"])
        key = (seg_id, f"audit:{finding.get('kind')}", str(finding.get("message")))
        if key in seen:
            continue
        seen.add(key)
        flagged.append(
            FlaggedIssue(
                f"a{len(flagged) + 1}",
                seg_id,
                chunk_of.get(seg_id, -1),
                f"audit:{finding.get('kind')}",
                str(finding.get("severity")),
                str(finding.get("message")),
                origin="audit",
            )
        )
    return flagged


def _chunk_of_segments(records: Sequence[dict]) -> dict[int, int]:
    owners: dict[int, int] = {}
    for record in records:
        for issue in (record.get("quality_report") or {}).get("issues") or []:
            if issue.get("segment") is not None:
                owners.setdefault(int(issue["segment"]), int(record.get("chunk_index", -1)))
    return owners


def needs_direct_handoff(issue: FlaggedIssue) -> str:
    """编辑修不了的问题直接转人工，返回原因；可以交给 agent 的返回空串。"""
    if issue.kind in _DIRECT_HANDOFF:
        return "该类问题需要重译或改结构，编辑修不了"
    if issue.kind.startswith("audit:") and issue.kind.split(":", 1)[1] in _DIRECT_HANDOFF_AUDIT:
        return "非文字对象（图片、公式、代码、表格、链接、脚注）对不上，需要人工核对"
    return ""


# --------------------------------------------------------------------------- #
# 约束式编辑
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReviewEdit:
    old: str
    new: str

    def to_dict(self) -> dict:
        return {"from": self.old, "to": self.new}


def parse_edits(raw: object) -> tuple[list[ReviewEdit], str]:
    """解析 agent 给出的编辑列表 [{"from": ..., "to": ...}]；返回 (编辑, 错误原因)。"""
    if not isinstance(raw, list) or not raw:
        return [], "edits 必须是非空列表"
    if len(raw) > MAX_EDITS:
        return [], f"一次最多 {MAX_EDITS} 条编辑"
    edits = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("from"), str) or not isinstance(item.get("to"), str):
            return [], '每条编辑必须是 {"from": 原文字, "to": 新文字}'
        edits.append(ReviewEdit(item["from"], item["to"]))
    return edits, ""


def _changed_middle(old: str, new: str) -> tuple[str, str]:
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(old), len(new)) - prefix and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]:
        suffix += 1
    return old[prefix : len(old) - suffix], new[prefix : len(new) - suffix]


def check_edit(edit: ReviewEdit, text: str) -> str:
    """校验一条编辑；合法返回空串，否则返回原因。"""
    if not edit.old:
        return "from 为空"
    if edit.old == edit.new:
        return "from 与 to 相同"
    count = text.count(edit.old)
    if count != 1:
        return "from 不在当前译文中" if count == 0 else "from 在译文中出现多次，请带上更多前后文"
    if any(mark in edit.old + edit.new for mark in "<>"):
        return "编辑不能涉及标签"
    old_mid, new_mid = _changed_middle(edit.old, edit.new)
    if max(len(old_mid), len(new_mid)) > MAX_EDIT_SPAN:
        return f"改动片段超过 {MAX_EDIT_SPAN} 字，审校只做局部修改，整段重写请转人工"
    return ""


def apply_review_edits(text: str, edits: Sequence[ReviewEdit]) -> tuple[str, list[str]]:
    """逐条校验并应用编辑；任何一条不合法则整体不应用，返回 (原文, 原因列表)。"""
    current = text
    problems = []
    for edit in edits:
        reason = check_edit(edit, current)
        if reason:
            problems.append(f"「{edit.old[:20]}」：{reason}")
            continue
        current = current.replace(edit.old, edit.new, 1)
    if problems:
        return text, problems
    return current, []


# --------------------------------------------------------------------------- #
# 生效判定
# --------------------------------------------------------------------------- #


def _signatures(issues: Iterable[IssueLike]) -> set[tuple[str, str]]:
    return {(issue.kind, issue.message) for issue in issues if issue.severity in SEVERE}


def judge_fix(target: FlaggedIssue, before: Sequence[IssueLike], after: Sequence[IssueLike]) -> tuple[bool, str]:
    """逐类型不退步：目标问题消失，且没有新增任何其他高/中严重度的问题。"""
    old, new = _signatures(before), _signatures(after)
    if (target.kind, target.message) in new:
        return False, "目标问题仍在：" + target.message[:60]
    same_kind_before = sum(1 for kind, _ in old if kind == target.kind)
    same_kind_after = sum(1 for kind, _ in new if kind == target.kind)
    if same_kind_before and same_kind_after >= same_kind_before:
        remaining = next(message for kind, message in sorted(new) if kind == target.kind)
        return False, "同类问题没有减少：" + remaining[:60]
    added = sorted(message for kind, message in new - old if kind != target.kind)
    if added:
        return False, "新增问题：" + "；".join(m[:60] for m in added[:2])
    return True, ""


def check_dismiss_reason(reason: object) -> str:
    if not isinstance(reason, str) or len(reason.strip()) < MIN_DISMISS_REASON:
        return f"驳回必须写明理由（至少 {MIN_DISMISS_REASON} 个字），说明为什么这是误报"
    return ""


# --------------------------------------------------------------------------- #
# 结局与汇总
# --------------------------------------------------------------------------- #


@dataclass
class TriageResult:
    issue: FlaggedIssue
    verdict: str
    reason: str = ""
    edits: list[ReviewEdit] = field(default_factory=list)
    before: str = ""
    after: str = ""  # 已修时的新译文（段级纯文本）
    by: str = "agent"  # agent | rule（直接转人工）| limit（超出本次分诊条数上限）| disabled（未启用 agent）
    steps: int = 0
    tokens: int = 0
    tool_calls: int = 0

    def to_dict(self) -> dict:
        return {
            "issue": self.issue.to_dict(),
            "verdict": self.verdict,
            "label": VERDICT_LABELS[self.verdict],
            "reason": self.reason,
            "edits": [edit.to_dict() for edit in self.edits],
            "before": self.before,
            "after": self.after,
            "by": self.by,
            "steps": self.steps,
            "tokens": self.tokens,
            "tool_calls": self.tool_calls,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TriageResult:
        edits = [ReviewEdit(item["from"], item["to"]) for item in data.get("edits", [])]
        fields = {
            key: data[key] for key in ("verdict", "reason", "before", "after", "by", "steps", "tokens", "tool_calls")
        }
        return cls(FlaggedIssue(**data["issue"]), edits=edits, **fields)


def summarize(results: Sequence[TriageResult]) -> dict:
    """分诊结果与成本汇总：各结局数量、按问题类型的分布、每条问题的步数与 token 上限和均值。"""
    by_verdict = Counter(result.verdict for result in results)
    agent = [result for result in results if result.by == "agent"]
    tokens = [result.tokens for result in agent]
    steps = [result.steps for result in agent]
    by_kind: dict[str, Counter] = {}
    for result in results:
        by_kind.setdefault(result.issue.kind, Counter())[result.verdict] += 1
    return {
        "issues": len(results),
        "by_verdict": {verdict: by_verdict.get(verdict, 0) for verdict in VERDICTS},
        "by_kind": {kind: dict(counter) for kind, counter in sorted(by_kind.items())},
        "direct_handoff": sum(1 for result in results if result.by == "rule"),
        "over_limit": sum(1 for result in results if result.by == "limit"),
        "agent_runs": len(agent),
        "tokens": sum(tokens),
        "steps": sum(steps),
        "tool_calls": sum(result.tool_calls for result in agent),
        "max_tokens_per_issue": max(tokens, default=0),
        "max_steps_per_issue": max(steps, default=0),
        "mean_tokens_per_issue": round(sum(tokens) / len(tokens), 1) if tokens else 0,
    }


def annotate_report(report: dict, results: Sequence[TriageResult]) -> dict:
    """把分诊结局写回一块的质检报告：已修的问题移除，已驳回的降为 low 并写明理由，待人工的加标记。

    报告里另存一份 review 列表（审校页据此展示）；passed 按剩余的高/中问题重新计算（语义提示不计）。
    """
    by_key = {(r.issue.seg_id, r.issue.message): r for r in results if r.issue.origin == "quality"}
    issues = []
    for issue in report.get("issues") or []:
        segment = issue.get("segment")
        message = _strip_segment_prefix(str(issue.get("message", "")))
        result = by_key.get((int(segment), message)) if segment is not None else None
        if result is None:
            issues.append(issue)
            continue
        if result.verdict == FIXED:
            continue
        updated = dict(issue)
        prefix = str(issue.get("message", ""))[: len(str(issue.get("message", ""))) - len(message)]
        if result.verdict == DISMISSED:
            updated["severity"] = "low"
            updated["message"] = f"{prefix}[已驳回] {message}（理由：{result.reason}）"
        else:
            updated["message"] = f"{prefix}[待人工] {message}" + (f"（{result.reason}）" if result.reason else "")
        issues.append(updated)
    annotated = {**report, "issues": issues}
    annotated["issue_count"] = len(issues)
    annotated["passed"] = not any(
        issue.get("severity") in SEVERE and issue.get("kind") != "semantic" for issue in issues
    )
    annotated["review"] = [result.to_dict() for result in results]
    return annotated
