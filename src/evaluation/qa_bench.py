"""
注入错误基准命令行：一条命令输出各类注入错误的召回率与原样段对上的误报率，不需要 API 密钥。

    translator-qa-bench                                   # 用仓库自带的段对，对比"规则"与"规则 + 语义"
    translator-qa-bench --records <run>/X.records.json \\
        --glossary data/glossaries/CPglossary.json        # 另在本地真实译文上统计
    translator-qa-bench --out docs/evals/results          # 同时写 qa_injection.md / .json
    translator-qa-bench --semantic-model                  # 语义检查另调用模型（需要密钥，逐段调用）
    translator-qa-bench --with-audit                      # 另加非文字审计（#19）的检测器列
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from .injection import (
    ERROR_LABELS,
    ERROR_TYPES,
    BenchmarkReport,
    Detector,
    audit_detector,
    load_dataset,
    pairs_from_records,
    rule_detector,
    run_benchmark,
)
from .semantic_bench import combine, routing_report, semantic_detector


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _code(text: str) -> str:
    """表格单元里的代码片段：双反引号包裹，可容纳内容里的 ```；竖线转义。"""
    return "`` " + text.replace("|", "\\|") + " ``"


def _render_dataset(name: str, reports: dict[str, BenchmarkReport], routing: dict | None) -> list[str]:
    detectors = list(reports)
    first = reports[detectors[0]]
    header = "| 错误类型 | 注入数 | " + " | ".join(f"{d} 检出 | {d} 召回" for d in detectors) + " |"
    align = "| --- | ---: | " + " | ".join("---: | ---:" for _ in detectors) + " |"
    lines = ["", f"## {name}", "", f"原样段对：{first.clean_pairs}", "", header, align]
    for error_type in ERROR_TYPES:
        cells = " | ".join(
            f"{reports[d].by_type[error_type].detected} | {_pct(reports[d].by_type[error_type].recall)}"
            for d in detectors
        )
        lines.append(f"| {ERROR_LABELS[error_type]} | {first.by_type[error_type].injected} | {cells} |")
    total = sum(t.injected for t in first.by_type.values())
    cells = " | ".join(
        f"{sum(t.detected for t in reports[d].by_type.values())} | {_pct(reports[d].overall_recall)}" for d in detectors
    )
    lines.append(f"| **合计** | {total} | {cells} |")
    fp = " | ".join(f"{reports[d].false_positives} | {_pct(reports[d].false_positive_rate)}" for d in detectors)
    lines.append(f"| **原样段对误报** | {first.clean_pairs} | {fp} |")

    if first.seed_results:
        lines += [
            "",
            "### 种子反例",
            "",
            "| 类型 | 原文 | 改坏的译文 | " + " | ".join(detectors) + " |",
            "| --- | --- | --- | " + " | ".join(":---:" for _ in detectors) + " |",
        ]
        for index, seed in enumerate(first.seed_results):
            marks = " | ".join("✅" if reports[d].seed_results[index]["detected"] else "❌" for d in detectors)
            source = seed["source"].replace("\n", " ")
            corrupted = seed["corrupted"].replace("\n", " ")
            lines.append(f"| {ERROR_LABELS[seed['error_type']]} | {_code(source)} | {_code(corrupted)} | {marks} |")

    for detector in detectors:
        examples = reports[detector].false_positive_examples
        if examples:
            lines += ["", f"### 误报示例（{detector}）", ""]
            for example in examples[:5]:
                lines.append(f"- {_code(example['source'][:80])} → {'；'.join(example['issues'])[:200]}")

    if routing:
        lines += [
            "",
            "### 模型语义检查的成本估算（未调用模型，按字符粗估 token）",
            "",
            f"- 段落：{routing['segments']}，其中高风险（含否定/情态/数字/长句）{routing['high_risk_segments']}",
            f"- 全部送检的提示 token ≈ {routing['estimated_prompt_tokens_all']}；"
            f"只送高风险段 ≈ {routing['estimated_prompt_tokens_high_risk']}"
            f"（对照：这些段原文 + 译文 ≈ {routing['source_plus_translation_tokens']} token）",
            "- 只查高风险段时模型召回率的上限（注入错误落在高风险段的比例）：",
            "",
            "| 错误类型 | 注入数 | 落在高风险段 | 上限 |",
            "| --- | ---: | ---: | ---: |",
        ]
        for error_type in ERROR_TYPES:
            stats = routing["by_type"][error_type]
            lines.append(
                f"| {ERROR_LABELS[error_type]} | {stats['injected']} | {stats['in_high_risk']} | "
                f"{_pct(stats['recall_ceiling'])} |"
            )
    return lines


def render_markdown(results: dict[str, dict], command: str) -> str:
    lines = [
        "# 注入错误基准：规则质检与语义质检",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        f"- 复现命令：`{command}`",
        "- 检出口径：改坏后的译文报出了原样译文上没有的问题。误报口径：原样段对上报出任何问题。",
        "- 注入错误比真实错误更规整，召回率只说明质检能否抓住这类已知错误，不是真实译文的检出率。",
        "- `rules+semantic` 的语义部分默认只有确定性启发式（不调用模型、不花 token）。",
    ]
    for name, result in results.items():
        lines += _render_dataset(name, result["reports"], result.get("routing"))
    return "\n".join(lines) + "\n"


def _detectors(use_model: bool, all_segments: bool, with_audit: bool = False) -> dict[str, Detector]:
    rules = rule_detector()
    checker = None
    if use_model:
        from ..infrastructure.llm.chat_model_factory import ChatModelFactory
        from ..pipelines.translate.semantic_checker import SemanticChecker
        from ..utils.config_loader import get_config

        llm = ChatModelFactory(get_config()).create_semantic_checker()
        checker = SemanticChecker(llm, use_model=True, high_risk_only=not all_segments)
    name = "rules+semantic(model)" if use_model else "rules+semantic"
    semantic = combine(rules, semantic_detector(checker))
    detectors = {"rules": rules, name: semantic}
    if with_audit:
        audit = audit_detector()
        detectors["rules+audit"] = combine(rules, audit)
        detectors[f"{name}+audit"] = combine(semantic, audit)
    return detectors


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(prog="translator-qa-bench", description="注入错误基准（默认无需密钥）")
    parser.add_argument("--dataset", type=Path, default=root / "docs/evals/datasets/injection_pairs.json")
    parser.add_argument("--records", type=Path, help="结构化翻译输出的 records.json，在本地真实译文上另跑一组")
    parser.add_argument("--glossary", type=Path, help="与 --records 搭配的术语表 JSON")
    parser.add_argument("--out", type=Path, help="写出 qa_injection.md 与 qa_injection.json 的目录")
    parser.add_argument("--semantic-model", action="store_true", help="语义检查另调用模型（需要密钥）")
    parser.add_argument("--all-segments", action="store_true", help="与 --semantic-model 搭配：模型检查全部段")
    parser.add_argument("--with-audit", action="store_true", help="另加全书非文字审计（#19）的检测器列（需要 pandoc）")
    args = parser.parse_args(argv)

    detectors = _detectors(args.semantic_model, args.all_segments, args.with_audit)
    pairs, seeds = load_dataset(json.loads(args.dataset.read_text(encoding="utf-8")))
    datasets = {"自带段对": (pairs, seeds)}
    command = "translator-qa-bench" + (" --with-audit" if args.with_audit else "")
    if args.records:
        glossary = json.loads(args.glossary.read_text(encoding="utf-8")) if args.glossary else {}
        records = json.loads(args.records.read_text(encoding="utf-8"))
        datasets[f"本地真实译文（{args.records.name}）"] = (pairs_from_records(records, glossary), [])
        command += f" --records {args.records.name}" + (f" --glossary {args.glossary.name}" if args.glossary else "")
    if args.semantic_model:
        command += " --semantic-model" + (" --all-segments" if args.all_segments else "")

    results = {
        name: {
            "reports": {
                detector_name: run_benchmark(data_pairs, detector, detector_name=detector_name, seed_cases=data_seeds)
                for detector_name, detector in detectors.items()
            },
            "routing": routing_report(data_pairs),
        }
        for name, (data_pairs, data_seeds) in datasets.items()
    }

    markdown = render_markdown(results, command)
    print(markdown)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "qa_injection.md").write_text(markdown, encoding="utf-8")
        payload = {
            name: {
                "detectors": {d: report.as_dict() for d, report in result["reports"].items()},
                "routing": result["routing"],
            }
            for name, result in results.items()
        }
        (args.out / "qa_injection.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
