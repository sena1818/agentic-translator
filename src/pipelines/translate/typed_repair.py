"""
结构化模式的分类型修复（规则见 src/domain/rules/typed_repair.py）。

一块里需要修的段按"路"批量送审：每一路一次模型调用，各段按固定顺序依次走完自己需要的路，
每走一路都重新质检，按"目标问题消失且不新增其他问题"决定是否采用。
标签变体、自创标签、多余闭合这类能确定性修的，先由程序修掉，不调用模型。
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ...domain.document import inline_codec
from ...domain.rules import typed_repair as rules
from .langchain_compat import StrOutputParser
from .prompt_builder import TranslationPromptBuilder

logger = logging.getLogger(__name__)

_SEG_RE = re.compile(r'<seg\s+id="?(\d+)"?\s*>(.*?)</seg>', re.DOTALL)
_ECHO_PREFIX = re.compile(r"^(?:修正后的?译文|当前译文|译文)[:：]\s*")
_EXPECTED_TERM = re.compile(r"预期译名 `([^`]+)`")


@dataclass
class RepairStats:
    """修复统计：按路记尝试、采用、拒绝次数，模型调用次数，以及确定性修复次数。"""

    model_calls: int = 0
    deterministic: int = 0
    attempted: Counter = field(default_factory=Counter)
    accepted: Counter = field(default_factory=Counter)
    rejected: Counter = field(default_factory=Counter)
    reasons: Counter = field(default_factory=Counter)

    def merge(self, other: RepairStats) -> None:
        self.model_calls += other.model_calls
        self.deterministic += other.deterministic
        self.attempted.update(other.attempted)
        self.accepted.update(other.accepted)
        self.rejected.update(other.rejected)
        self.reasons.update(other.reasons)

    def as_dict(self) -> dict:
        attempted = sum(self.attempted.values())
        return {
            "model_calls": self.model_calls,
            "deterministic_fixes": self.deterministic,
            "attempted": dict(self.attempted),
            "accepted": dict(self.accepted),
            "rejected": dict(self.rejected),
            "success_rate": round(sum(self.accepted.values()) / attempted, 4) if attempted else None,
            "top_rejection_reasons": self.reasons.most_common(5),
        }


@dataclass
class RepairResult:
    repaired: bool
    stats: RepairStats
    rejections: dict[int, list[str]]  # local_id → 被拒绝的修复说明（供质检报告标记待人工处理）


def _segment_block(seg, outcome, route: str) -> str:
    kinds = rules.ROUTE_KINDS[route]
    problems = "；".join(
        issue.message for issue in outcome.issues if issue.kind in kinds and issue.severity in rules.SEVERE
    )
    current = outcome.tagged if outcome.tagged is not None else "（缺失）"
    return f"【段 {seg.local_id}】\n原文：{seg.tagged}\n当前译文：{current}\n问题：{problems}"


def _plain(outcome) -> str | None:
    return inline_codec.plain_text(outcome.inlines) if outcome.inlines is not None else None


class TypedRepairer:
    def __init__(
        self,
        llm_checker,
        prompt_builder: TranslationPromptBuilder,
        invoke: Callable[..., Awaitable[tuple[str, int, float]]],
    ):
        self.llm_checker = llm_checker
        self.prompt_builder = prompt_builder
        self.invoke = invoke

    async def repair(self, segments, outcomes: dict, candidates, check, common: dict, rounds: int) -> RepairResult:
        """就地更新 outcomes。check(seg, tagged) 返回重新质检后的 SegmentOutcome。"""
        stats = RepairStats()
        rejections: dict[int, list[str]] = {}
        repaired = self._deterministic(segments, outcomes, candidates, check, stats)

        for _ in range(rounds):
            for route in rules.ROUTE_ORDER:
                targets = [seg for seg in segments if route in rules.routes_needed(outcomes[seg.local_id].issues)]
                if not targets:
                    continue
                prompt = self.prompt_builder.build_typed_repair_prompt(route)
                payload = {
                    **common,
                    "segments": "\n".join(_segment_block(s, outcomes[s.local_id], route) for s in targets),
                }
                try:
                    raw, _, _ = await self.invoke(prompt | self.llm_checker | StrOutputParser(), payload)
                except Exception as exc:  # 审校失败不影响初译
                    logger.warning("段级%s修复失败，保留当前译文等待人工复核: %s", rules.ROUTE_LABELS[route], exc)
                    return RepairResult(repaired, stats, rejections)
                stats.model_calls += 1
                replies = self._parse(route, raw)
                for seg in targets:
                    stats.attempted[route] += 1
                    reason = self._try(route, seg, outcomes, check, replies.get(seg.local_id))
                    if reason:
                        stats.rejected[route] += 1
                        stats.reasons[f"{route}: {reason.split('：', 1)[0]}"] += 1
                        rejections.setdefault(seg.local_id, []).append(
                            f"{rules.ROUTE_LABELS[route]}修复未采用（{reason}）"
                        )
                    else:
                        stats.accepted[route] += 1
                        repaired = True
        return RepairResult(repaired, stats, rejections)

    def _deterministic(self, segments, outcomes, candidates, check, stats: RepairStats) -> bool:
        changed = False
        for seg in segments:
            outcome = outcomes[seg.local_id]
            if outcome.tagged is None:
                continue
            cleaned = inline_codec.canonicalize_tags(outcome.tagged, candidates[seg.candidate_index])
            if cleaned == outcome.tagged:
                continue
            candidate = check(seg, cleaned)
            if not rules.accept_repair("structure", outcome.issues, candidate.issues)[1].startswith("新增"):
                outcomes[seg.local_id] = candidate
                stats.deterministic += 1
                changed = True
        return changed

    @staticmethod
    def _parse(route: str, raw: str) -> dict:
        if route in rules.EDIT_ROUTES:
            return rules.parse_edits(raw)
        found: dict[int, str] = {}
        for match in _SEG_RE.finditer(raw):
            found.setdefault(int(match.group(1)), _ECHO_PREFIX.sub("", match.group(2).strip()))
        return found

    @staticmethod
    def _try(route: str, seg, outcomes: dict, check, reply) -> str:
        """尝试一条修复；采用则更新 outcomes 并返回空串，否则返回拒绝原因。"""
        before = outcomes[seg.local_id]
        if reply is None:
            return "模型未返回该段"
        if route in rules.EDIT_ROUTES:
            if before.tagged is None:
                return "该段没有可编辑的译文"
            renderings = [m for issue in before.issues for m in _EXPECTED_TERM.findall(issue.message)]
            text, problems = rules.apply_edits(route, before.tagged, reply, renderings)
            if text == before.tagged:
                return "编辑无效：" + ("；".join(problems[:2]) or "没有改动")
        else:
            text = reply
        candidate = check(seg, text)
        if route == "structure":
            old_plain, new_plain = _plain(before), _plain(candidate)
            if old_plain is not None and new_plain is not None and not rules.text_unchanged(old_plain, new_plain):
                return "标签修复改动了文字"
        accepted, reason = rules.accept_repair(route, before.issues, candidate.issues)
        if not accepted:
            return reason
        outcomes[seg.local_id] = candidate
        return ""
