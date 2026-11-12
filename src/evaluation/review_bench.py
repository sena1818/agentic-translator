"""
审校 agent 分诊基准：在"注入错误 + 已知误报"的混合集上统计正确修复、正确驳回、转交人工的比例。

    translator-review-bench                       # 不需要密钥：对比"不开 agent"与脚本化假审校员
    translator-review-bench --model config        # 用配置里的审校模型跑真实 agent（需要密钥）
    translator-review-bench --out docs/evals/results

混合集：
- 注入错误：#11 自带段对按七类注入错误，取质检（规则 + 语义启发式，有 pandoc 时再加非文字审计）
  新报出的第一条问题作为待分诊问题；正确结局是修复，且修后译文与原样译文一致。
- 已知误报：datasets/review_false_positives.json，译文正确但质检会报；正确结局是驳回。

脚本化假审校员知道标准答案，但按固定节奏犯两种错：每 4 条真错误里有 1 条先交一个没修好的编辑
（被不退步检查拒绝后转人工）；每 3 条术语误报里有 1 条想靠硬塞术语译名让报错消失。
它只用来检验分诊流程和检查闸门，不代表真实模型的分诊能力；真实模型的数字需要 --model config。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..application.tools.triage import review_registry
from ..domain.review.triage import (
    DISMISSED,
    FIXED,
    HANDOFF,
    SEVERE,
    CheckedIssue,
    FlaggedIssue,
    judge_fix,
)
from ..infrastructure.agent import MemoryTracer, ModelReply
from ..pipelines.review.review_agent import ReviewAgent, ReviewAgentConfig, TriageRun, untriaged
from ..pipelines.review.workspace import ReviewWorkspace, segment_recheck
from .injection import inject_all, load_dataset

TRUTH_FIX = "fix"
TRUTH_DISMISS = "dismiss"


@dataclass(frozen=True)
class BenchCase:
    issue: FlaggedIssue
    truth: str  # fix | dismiss
    reference: str  # 正确译文（注入错误前的原样译文；误报则就是当前译文）
    current: str
    error_type: str = ""  # 注入错误类型；误报为 false_positive
    note: str = ""


def bench_recheck(with_audit: bool):
    base = segment_recheck()
    audit = None
    if with_audit:
        from .injection import audit_detector

        audit = audit_detector()

    def recheck(source: str, translation: str, glossary: dict[str, str]) -> list[CheckedIssue]:
        issues = base(source, translation, glossary)
        if audit is not None:
            issues += [
                CheckedIssue(kind, "high", message) for kind, message in sorted(audit(source, translation, glossary))
            ]
        return issues

    return recheck


def _severe(issues) -> set[tuple[str, str]]:
    return {(issue.kind, issue.message) for issue in issues if issue.severity in SEVERE}


def build_cases(root: Path, with_audit: bool) -> tuple[list[BenchCase], dict[str, str], list[tuple[str, str]], dict]:
    """返回 (待分诊用例, 合并术语表, 段对列表（段号 = 下标 + 1）, 统计)。"""
    pairs, _ = load_dataset(json.loads((root / "docs/evals/datasets/injection_pairs.json").read_text(encoding="utf-8")))
    false_positives = json.loads((root / "docs/evals/datasets/review_false_positives.json").read_text(encoding="utf-8"))
    glossary = {**(pairs[0].glossary if pairs else {}), **false_positives.get("glossary", {})}
    recheck = bench_recheck(with_audit)

    cases: list[BenchCase] = []
    segments: list[tuple[str, str]] = []
    undetected = 0
    for injected in inject_all(pairs):
        clean = _severe(recheck(injected.pair.source, injected.pair.translation, glossary))
        new = sorted(_severe(recheck(injected.pair.source, injected.corrupted, glossary)) - clean)
        if not new:
            undetected += 1
            continue
        segments.append((injected.pair.source, injected.corrupted))
        kind, message = new[0]
        severity = next(
            i.severity
            for i in recheck(injected.pair.source, injected.corrupted, glossary)
            if (i.kind, i.message) == (kind, message)
        )
        issue = FlaggedIssue(f"i{len(cases) + 1}", len(segments), len(segments) - 1, kind, severity, message)
        cases.append(
            BenchCase(
                issue, TRUTH_FIX, injected.pair.translation, injected.corrupted, injected.error_type, injected.note
            )
        )
    unflagged = 0
    for item in false_positives["cases"]:
        flagged = sorted(_severe(recheck(item["source"], item["translation"], glossary)))
        if not flagged:
            unflagged += 1
            continue
        segments.append((item["source"], item["translation"]))
        kind, message = flagged[0]
        issue = FlaggedIssue(f"f{len(cases) + 1}", len(segments), len(segments) - 1, kind, "medium", message)
        cases.append(
            BenchCase(issue, TRUTH_DISMISS, item["translation"], item["translation"], "false_positive", item["note"])
        )
    stats = {"undetected_injections": undetected, "false_positives_not_flagged": unflagged}
    return cases, glossary, segments, stats


# --------------------------------------------------------------------------- #
# 脚本化假审校员
# --------------------------------------------------------------------------- #


def minimal_edit(current: str, reference: str) -> dict:
    """把 current 改成 reference 的最小编辑：去掉公共前后缀，再向两侧扩展到 from 非空且在 current 中唯一。"""
    prefix = 0
    while prefix < min(len(current), len(reference)) and current[prefix] == reference[prefix]:
        prefix += 1
    suffix = 0
    limit = min(len(current), len(reference)) - prefix
    while suffix < limit and current[len(current) - 1 - suffix] == reference[len(reference) - 1 - suffix]:
        suffix += 1
    start, end = prefix, len(current) - suffix
    while not current[start:end] or current.count(current[start:end]) != 1:
        if start == 0 and end == len(current):
            break
        start, end = max(0, start - 1), min(len(current), end + 1)
    tail = len(current) - end
    return {"from": current[start:end], "to": reference[start : len(reference) - tail]}


def _tool(name: str, args: dict | None = None) -> str:
    return json.dumps({"thought": "", "action": "tool", "tool": name, "args": args or {}}, ensure_ascii=False)


def _submit(conclusion: dict, evidence: list[str]) -> str:
    return json.dumps(
        {"thought": "", "action": "submit", "conclusion": conclusion, "evidence": evidence}, ensure_ascii=False
    )


def _handoff(reason: str) -> str:
    return json.dumps({"thought": "", "action": "handoff", "reason": reason}, ensure_ascii=False)


class ScriptedReviewer:
    """知道标准答案、按固定节奏犯错的假审校员（见模块说明）。"""

    def __init__(self, cases: list[BenchCase], glossary: dict[str, str]):
        self.scripts: dict[str, list[str]] = {}
        fixes = dismisses = 0
        for case in cases:
            if case.truth == TRUTH_FIX:
                fixes += 1
                good = minimal_edit(case.current, case.reference)
                if fixes % 4 == 0:  # 交一个没修好的编辑：只在句首加两个字，目标问题还在
                    bad = {"from": case.current[:2], "to": "其实" + case.current[:2]}
                    script = [_tool("get_issue"), _submit({"verdict": "fix", "edits": [bad]}, ["E1"])]
                    script.append(_handoff("修改没通过检查，拿不准正确译法"))
                else:
                    script = [
                        _tool("get_issue"),
                        _tool("check_fix", {"edits": [good]}),
                        _submit({"verdict": "fix", "edits": [good]}, ["E2"]),
                    ]
            else:
                dismisses += 1
                term = case.issue.message.split("`")[3] if case.issue.kind == "terminology" else ""
                if term and dismisses % 3 == 0:  # 想靠硬塞术语译名让报错消失
                    tail = case.current[-3:]
                    stuffed = {"from": tail, "to": f"{tail[:-1]}（{term}）{tail[-1]}"}
                    script = [_tool("get_issue"), _submit({"verdict": "fix", "edits": [stuffed]}, ["E1"])]
                    script.append(_submit({"verdict": "dismiss", "reason": case.note or "普通词义，不是术语"}, ["E1"]))
                else:
                    script = [
                        _tool("get_issue"),
                        _submit({"verdict": "dismiss", "reason": case.note or "译文正确，质检误报"}, ["E1"]),
                    ]
            self.scripts[case.issue.issue_id] = script

    async def __call__(self, messages, *, metadata: dict | None = None) -> ModelReply:
        metadata = metadata or {}
        script = self.scripts[metadata["item"]]
        step = int(metadata.get("step", 1)) - 1
        return ModelReply(script[min(step, len(script) - 1)])


# --------------------------------------------------------------------------- #
# 评分
# --------------------------------------------------------------------------- #


def _norm(text: str) -> str:
    return "".join(text.split())


def score(cases: list[BenchCase], run: TriageRun, workspace: ReviewWorkspace, rejected: int) -> dict:
    by_id = {case.issue.issue_id: case for case in cases}
    outcome = {
        "correct_fix": 0,
        "fix_differs": 0,
        "wrong_dismiss": 0,
        "fix_handoff": 0,
        "correct_dismiss": 0,
        "wrong_fix": 0,
        "dismiss_handoff": 0,
    }
    gate_ok = True
    wrong_fix_examples = []
    for result in run.results:
        case = by_id[result.issue.issue_id]
        if result.verdict == FIXED:
            before = workspace.check(case.issue.seg_id, result.before)
            after = workspace.check(case.issue.seg_id, result.after)
            gate_ok = gate_ok and judge_fix(case.issue, before, after)[0]
        if case.truth == TRUTH_FIX:
            if result.verdict == FIXED:
                outcome["correct_fix" if _norm(result.after) == _norm(case.reference) else "fix_differs"] += 1
            elif result.verdict == DISMISSED:
                outcome["wrong_dismiss"] += 1
            else:
                outcome["fix_handoff"] += 1
        elif result.verdict == DISMISSED:
            outcome["correct_dismiss"] += 1
        elif result.verdict == FIXED:
            outcome["wrong_fix"] += 1
            wrong_fix_examples.append({"before": result.before, "after": result.after})
        else:
            outcome["dismiss_handoff"] += 1
    n_fix = sum(1 for case in cases if case.truth == TRUTH_FIX)
    n_dismiss = len(cases) - n_fix
    handoffs = sum(1 for result in run.results if result.verdict == HANDOFF)

    def rate(value: int, total: int) -> float | None:
        return round(value / total, 4) if total else None

    return {
        "cases": {"injected_errors": n_fix, "known_false_positives": n_dismiss},
        "outcome": outcome,
        "rates": {
            "correct_fix": rate(outcome["correct_fix"], n_fix),
            "correct_dismiss": rate(outcome["correct_dismiss"], n_dismiss),
            "handoff": rate(handoffs, len(cases)),
            "wrong_fix_on_false_positive": rate(outcome["wrong_fix"], n_dismiss),
            "wrong_dismiss_of_real_error": rate(outcome["wrong_dismiss"], n_fix),
        },
        "gate": {
            "fixes_applied": sum(1 for result in run.results if result.verdict == FIXED),
            "all_applied_fixes_pass_no_regression": gate_ok,
            "rejected_submissions": rejected,
        },
        "cost": run.summary,
        "wrong_fix_examples": wrong_fix_examples[:3],
    }


async def run_policy(cases, glossary, segments, model, with_audit: bool, config: ReviewAgentConfig) -> dict:
    """model 为 None 时是关闭 agent 的现状：不调用模型，全部留给人工。"""
    flagged = [case.issue for case in cases]
    workspace = ReviewWorkspace.from_pairs(segments, glossary, flagged, bench_recheck(with_audit))
    tracer = MemoryTracer()
    if model is None:
        return score(cases, untriaged(workspace), workspace, 0)
    run = await ReviewAgent(model, review_registry, config, tracer).triage(workspace)
    rejected = sum(1 for event in tracer.events if event["type"] == "rejected")
    return score(cases, run, workspace, rejected)


def _real_model(config_path: Path | None):
    from ..infrastructure.agent import RunnableChatModel
    from ..infrastructure.llm.chat_model_factory import ChatModelFactory
    from ..utils.config_loader import get_config

    config = get_config(str(config_path) if config_path else None)
    return RunnableChatModel(ChatModelFactory(config).create_checker(), run_name="review-agent")


def _pct(value) -> str:
    return "—" if value is None else f"{value:.0%}"


def render(results: dict, stats: dict, command: str) -> str:
    lines = [
        "# 审校 agent 分诊基准",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        f"- 复现命令：`{command}`",
        f"- 未被质检报出、因而不进入分诊的注入错误：{stats['undetected_injections']}；"
        f"质检没有报的误报样例：{stats['false_positives_not_flagged']}",
        "- 脚本化假审校员知道标准答案、按固定节奏犯错，只检验分诊流程与不退步闸门，不代表真实模型的能力。",
        "",
        "| 策略 | 正确修复 | 正确驳回 | 转交人工 | 误报被改 | 真错被驳回 | 生效修改全部通过检查 | 被闸门拒绝的提交 | 每条最多 token | 总 token |",
        "| --- | ---: | ---: | ---: | ---: | ---: | :---: | ---: | ---: | ---: |",
    ]
    for name, result in results.items():
        rates, gate, cost = result["rates"], result["gate"], result["cost"]
        lines.append(
            f"| {name} | {_pct(rates['correct_fix'])} | {_pct(rates['correct_dismiss'])} | {_pct(rates['handoff'])} "
            f"| {_pct(rates['wrong_fix_on_false_positive'])} | {_pct(rates['wrong_dismiss_of_real_error'])} "
            f"| {'✅' if gate['all_applied_fixes_pass_no_regression'] else '❌'} | {gate['rejected_submissions']} "
            f"| {cost['max_tokens_per_issue']} | {cost['tokens']} |"
        )
    first = next(iter(results.values()))
    lines += [
        "",
        f"混合集：注入错误 {first['cases']['injected_errors']} 条，已知误报 {first['cases']['known_false_positives']} 条。",
    ]
    for name, result in results.items():
        lines += ["", f"## {name}", "", "```json", json.dumps(result["outcome"], ensure_ascii=False), "```"]
        by_kind = result["cost"]["by_kind"]
        lines.append("按问题类型：" + "；".join(f"{kind} {dict(v)}" for kind, v in by_kind.items()))
        for example in result.get("wrong_fix_examples", []):
            lines.append(f"- 误报被改：{example['before']} → {example['after']}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(prog="translator-review-bench", description="审校 agent 分诊基准")
    parser.add_argument("--model", choices=["scripted", "config"], default="scripted")
    parser.add_argument("--config", type=Path, help="--model config 时使用的配置文件")
    parser.add_argument("--no-audit", action="store_true", help="重新质检不含非文字审计（没有 pandoc 时自动关闭）")
    parser.add_argument("--out", type=Path, help="写出 review_triage.md / .json 的目录")
    args = parser.parse_args(argv)

    from ..infrastructure.document import pandoc

    with_audit = not args.no_audit and pandoc.pandoc_available()
    cases, glossary, segments, stats = build_cases(root, with_audit)
    config = ReviewAgentConfig(enabled=True)
    policies = {"不开 agent（现状）": None}
    if args.model == "scripted":
        policies["脚本化假审校员"] = ScriptedReviewer(cases, glossary)
    else:
        policies["审校 agent（配置中的模型）"] = _real_model(args.config)
    results = {
        name: asyncio.run(run_policy(cases, glossary, segments, model, with_audit, config))
        for name, model in policies.items()
    }
    command = "translator-review-bench" + ("" if args.model == "scripted" else " --model config")
    markdown = render(results, stats, command)
    print(markdown)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "review_triage.md").write_text(markdown, encoding="utf-8")
        payload = {"stats": stats, "with_audit": with_audit, "policies": results}
        (args.out / "review_triage.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
