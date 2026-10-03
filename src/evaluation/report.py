"""把评测结果渲染成 Markdown 报告与原始 JSON。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from .consistency import ConsistencyReport
from .models import ComparisonGroup


@dataclass
class CostSummary:
    """评测的 token 与花费汇总。"""

    total_tokens: int = 0
    total_calls: int = 0
    estimated_cost_rmb: float | None = None  # 需按实际单价填写；None 表示待补

    def as_dict(self) -> dict:
        return {
            "total_tokens": self.total_tokens,
            "total_calls": self.total_calls,
            "estimated_cost_rmb": self.estimated_cost_rmb,
        }


@dataclass
class EvalReport:
    """一次完整评测的产物。"""

    comparisons: list[ComparisonGroup]
    consistency: ConsistencyReport
    cost: CostSummary
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "meta": self.meta,
            "comparisons": [c.as_dict() for c in self.comparisons],
            "consistency": self.consistency.as_dict(),
            "cost": self.cost.as_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2)


def render_comparison_table(group: ComparisonGroup) -> str:
    """把一组对照渲染成 Markdown 分数表。"""
    lines = [
        f"### {group.title}",
        "",
        "| 变体 | 样本数 | 准确性 | 流畅度 | 术语一致性 | 综合 | 总 token | 平均延迟(s) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for v in group.variants:
        lines.append(
            f"| {v.label} | {v.n} | {v.mean_accuracy:.2f} | {v.mean_fluency:.2f} "
            f"| {v.mean_terminology:.2f} | {v.mean_overall:.2f} | {v.total_tokens} | {v.mean_latency_s:.2f} |"
        )
    return "\n".join(lines)


def render_consistency(report: ConsistencyReport) -> str:
    """渲染术语表外一致率与 RAG 决策。"""
    rate_text = f"{report.rate:.2%}" if report.rate is not None else "样本不足"
    decision_text = {
        "insufficient-data": "样本不足，暂不作 RAG 决策。",
        "no-rag": f"一致率 {rate_text} ≥ 门槛 {report.threshold:.0%}，**不立项 RAG**，以 ADR 记录。",
        "build-rag": f"一致率 {rate_text} < 门槛 {report.threshold:.0%}，**立项文档内翻译记忆（sqlite-vec）**，另起 PRD。",
    }[report.decision]

    lines = [
        "## 术语表外重复短语一致率（RAG 门槛）",
        "",
        f"- 候选重复短语：{report.total_phrases}",
        f"- 译法一致短语：{report.consistent_phrases}",
        f"- 一致率：**{rate_text}**（门槛 {report.threshold:.0%}）",
        f"- 决策：{decision_text}",
    ]
    if report.total_phrases == 0:
        lines.append(
            "- 说明：候选为 0（语料过短、无跨块重复短语，或全被术语表覆盖）；"
            "较长文档请用 `--consistency-doc` 指定英文源文。"
        )
    if report.phrases:
        lines += [
            "",
            "| 重复短语 | 出现次数 | 一致 | 译法签名 |",
            "| --- | ---: | :---: | --- |",
        ]
        for p in report.phrases[:30]:
            mark = "✅" if p.consistent else "❌"
            lines.append(f"| {p.phrase} | {p.occurrences} | {mark} | {p.signature or '—'} |")
    return "\n".join(lines)


def render_report(report: EvalReport) -> str:
    """渲染完整 Markdown 评测报告。"""
    meta = report.meta
    header = [
        "# 翻译质量评测报告",
        "",
        f"- 生成日期：{meta.get('date', date.today().isoformat())}",
        f"- 裁判模型：{meta.get('judge_model', '（未记录）')}",
        f"- 选手模型：{meta.get('candidate_model', '（未记录）')}",
        f"- 数据集：{meta.get('dataset', '（未记录）')}",
        f"- 复现命令：`{meta.get('command', 'translator-eval')}`",
    ]
    if meta.get("note"):
        header += ["", f"> {meta['note']}"]

    sections = ["\n".join(header), "", "## 三组对照分数表"]
    for group in report.comparisons:
        sections.append("")
        sections.append(render_comparison_table(group))

    sections += ["", render_consistency(report.consistency)]

    cost = report.cost
    cost_line = f"{cost.estimated_cost_rmb:.2f} 元" if cost.estimated_cost_rmb is not None else "（按实际单价填写）"
    sections += [
        "",
        "## 成本",
        "",
        f"- 总 token：{cost.total_tokens}",
        f"- 总调用次数：{cost.total_calls}",
        f"- 估算 API 花费：{cost_line}",
    ]
    return "\n".join(sections) + "\n"
