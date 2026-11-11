"""
语义质检器：否定、情态强弱、数字角色、漏译与增译。

两层：
1. 确定性启发式（domain/rules/semantic_signals.py），全量段落都跑，不花 token；
2. 可选的模型检查：按段批量送给一个（最好与翻译员异源的）模型，要求结构化输出
   原文片段、译文片段、类型、严重度、理由，允许"无法判定"。可配置为只查高风险段
   （含否定、情态、数字或长句）以控制成本。

两层的结论都以 kind="semantic" 的质检问题进入报告，消息前缀标明"语义提示·规则/模型"：
它们是提示，不是正确性保证。本阶段在段级修复之后运行，只报告、不触发自动修复。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ...core.validator import QualityIssue
from ...domain.rules.semantic_signals import SEMANTIC_TYPES, SemanticFinding, heuristic_findings, is_high_risk
from ...infrastructure.llm.retry import ainvoke_with_retries

logger = logging.getLogger(__name__)

_SEVERITIES = {"high", "medium", "low"}

SEMANTIC_PROMPT = """你是严格的中英翻译审校员，只检查语义保真，不评价文风。
逐段对照原文与译文，只找下面几类问题：
- negation：否定被反转、丢失或作用范围改变
- modality：情态或论证强度被改变（may/suggest 译成"证明/必然"，must 译成"可以"等）
- number_role：数字的角色、方向、比较对象或单位被改变（起止写反、涨跌写反、数字配错对象）
- omission：漏译了原文的分句、限定语或论点
- addition：增加了原文没有的信息
- uncertain：疑似有上述问题但无法确定

没有问题的段不要输出。只输出一个 JSON 对象，不要输出其他文字：
{{"findings": [{{"id": 段编号, "type": "上面六类之一", "severity": "high|medium|low",
"source_span": "原文中相关片段", "target_span": "译文中相关片段", "reason": "一句话理由"}}]}}

待检查段落：
{segments}
"""


@dataclass(frozen=True)
class SegmentPair:
    key: int
    source: str
    translation: str


@dataclass
class SemanticUsage:
    """语义检查阶段的成本：单独统计，便于和翻译本身的成本对比。"""

    segments_total: int = 0
    segments_sent: int = 0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_estimated: bool = False
    invalid_responses: int = 0
    seconds: float = 0.0

    def add(self, other: SemanticUsage) -> None:
        for name in ("segments_total", "segments_sent", "calls", "input_tokens", "output_tokens", "invalid_responses"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.tokens_estimated = self.tokens_estimated or other.tokens_estimated
        self.seconds = round(self.seconds + other.seconds, 3)

    def as_dict(self) -> dict:
        return {
            "segments_total": self.segments_total,
            "segments_sent": self.segments_sent,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tokens_estimated": self.tokens_estimated,
            "invalid_responses": self.invalid_responses,
            "seconds": self.seconds,
        }


@dataclass
class SemanticResult:
    findings: dict[int, list[SemanticFinding]] = field(default_factory=dict)
    usage: SemanticUsage = field(default_factory=SemanticUsage)


def estimate_tokens(text: str) -> int:
    """无用量回报时的粗估：汉字约 1 token/字，其余约 4 字符/token。"""
    cjk = len(re.findall(r"[一-鿿]", text))
    return cjk + (len(text) - cjk + 3) // 4


def build_prompt(pairs: list[SegmentPair]) -> str:
    body = "\n\n".join(f"【段 {pair.key}】\n原文：{pair.source}\n译文：{pair.translation}" for pair in pairs)
    return SEMANTIC_PROMPT.format(segments=body)


def parse_findings(raw: str, valid_keys: set[int]) -> list[tuple[int, SemanticFinding]]:
    """解析模型输出；格式错误抛 ValueError。未知类型归为"无法判定"，未知段编号丢弃。"""
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError(f"语义检查输出中没有 JSON 对象：{raw[:80]!r}")
    data = json.loads(match.group(0))
    items = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("语义检查输出缺少 findings 列表")
    parsed = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            key = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if key not in valid_keys:
            continue
        kind = str(item.get("type", "")).strip()
        if kind not in SEMANTIC_TYPES:
            kind = "uncertain"
        severity = str(item.get("severity", "")).strip().lower()
        if severity not in _SEVERITIES or kind == "uncertain":
            severity = "low"
        parsed.append(
            (
                key,
                SemanticFinding(
                    kind,
                    severity,
                    str(item.get("reason", "")).strip() or "模型未给出理由",
                    str(item.get("source_span", "")).strip(),
                    str(item.get("target_span", "")).strip(),
                    origin="model",
                ),
            )
        )
    return parsed


def _response_text(result) -> tuple[str, dict | None]:
    if isinstance(result, str):
        return result, None
    content = getattr(result, "content", result)
    if isinstance(content, list):
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return str(content), getattr(result, "usage_metadata", None)


class SemanticChecker:
    """段级语义检查。llm 为 None 或 use_model=False 时只跑确定性启发式。"""

    def __init__(
        self,
        llm=None,
        *,
        use_model: bool = False,
        high_risk_only: bool = True,
        batch_size: int = 12,
        max_retries: int = 2,
        before_call: Callable[[], Awaitable[None]] | None = None,
    ):
        self.llm = llm
        self.use_model = use_model and llm is not None
        self.high_risk_only = high_risk_only
        self.batch_size = max(1, batch_size)
        self.max_retries = max_retries
        self.before_call = before_call
        self.usage = SemanticUsage()

    @classmethod
    def from_config(cls, config, llm=None, before_call=None) -> SemanticChecker | None:
        """quality.semantic_check.enabled 为 false 时返回 None（行为与未接入时一致）。"""
        if not config.get("quality.semantic_check.enabled", False):
            return None
        return cls(
            llm,
            use_model=bool(config.get("quality.semantic_check.use_model", False)),
            high_risk_only=bool(config.get("quality.semantic_check.high_risk_only", True)),
            batch_size=int(config.get("quality.semantic_check.batch_size", 12)),
            max_retries=int(config.get("api.translator.max_retries", 2)),
            before_call=before_call,
        )

    def select(self, pairs: list[SegmentPair]) -> list[SegmentPair]:
        """要送模型检查的段：全部，或只含高风险信号的段。"""
        return [pair for pair in pairs if not self.high_risk_only or is_high_risk(pair.source)]

    async def check(self, pairs: list[SegmentPair]) -> SemanticResult:
        result = SemanticResult()
        result.usage.segments_total = len(pairs)
        for pair in pairs:
            found = heuristic_findings(pair.source, pair.translation)
            if found:
                result.findings.setdefault(pair.key, []).extend(found)
        if self.use_model:
            selected = self.select(pairs)
            for start in range(0, len(selected), self.batch_size):
                await self._check_batch(selected[start : start + self.batch_size], result)
        self.usage.add(result.usage)
        return result

    async def _check_batch(self, batch: list[SegmentPair], result: SemanticResult) -> None:
        prompt = build_prompt(batch)
        started = time.monotonic()
        try:
            response, _ = await ainvoke_with_retries(self.llm, prompt, self.max_retries, self.before_call)
        except Exception as exc:  # 语义检查失败不影响译文
            logger.warning("语义检查调用失败，跳过本批 %d 段: %s", len(batch), exc)
            result.usage.invalid_responses += 1
            return
        finally:
            result.usage.seconds = round(result.usage.seconds + time.monotonic() - started, 3)
        raw, usage = _response_text(response)
        result.usage.calls += 1
        result.usage.segments_sent += len(batch)
        if usage and usage.get("input_tokens") is not None:
            result.usage.input_tokens += int(usage.get("input_tokens") or 0)
            result.usage.output_tokens += int(usage.get("output_tokens") or 0)
        else:
            result.usage.input_tokens += estimate_tokens(prompt)
            result.usage.output_tokens += estimate_tokens(raw)
            result.usage.tokens_estimated = True
        try:
            parsed = parse_findings(raw, {pair.key for pair in batch})
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning("语义检查输出无法解析，本批 %d 段记为未检查: %s", len(batch), exc)
            result.usage.invalid_responses += 1
            return
        for key, finding in parsed:
            result.findings.setdefault(key, []).append(finding)


def findings_to_issues(findings: list[SemanticFinding], segment: int | None = None) -> list[QualityIssue]:
    """语义提示转成质检问题：kind 统一为 semantic，消息标明来源是规则还是模型。"""
    return [QualityIssue("semantic", finding.severity, finding.message(), segment) for finding in findings]
