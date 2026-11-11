"""
注入错误基准命令行：一条命令输出各类注入错误的召回率与原样段对上的误报率，不需要 API 密钥。

    translator-qa-bench                                   # 用仓库自带的段对
    translator-qa-bench --records <run>/X.records.json \\
        --glossary data/glossaries/CPglossary.json        # 另在本地真实译文上统计
    translator-qa-bench --out docs/evals/results          # 同时写 qa_injection.md / .json
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
    audit_detector,
    combined_detector,
    load_dataset,
    pairs_from_records,
    rule_detector,
    run_benchmark,
)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _code(text: str) -> str:
    """表格单元里的代码片段：双反引号包裹，可容纳内容里的 ```；竖线转义。"""
    return "`` " + text.replace("|", "\\|") + " ``"


def render_markdown(reports: dict[str, BenchmarkReport], command: str, title: str = "规则质检") -> str:
    lines = [
        f"# 注入错误基准：{title}",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        f"- 复现命令：`{command}`",
        "- 检出口径：改坏后的译文报出了原样译文上没有的问题。误报口径：原样段对上报出任何问题。",
        "- 注入错误比真实错误更规整，召回率只说明质检能否抓住这类已知错误，不是真实译文的检出率。",
    ]
    for name, report in reports.items():
        lines += [
            "",
            f"## {name}",
            "",
            f"- 原样段对：{report.clean_pairs}，误报 {report.false_positives}（{_pct(report.false_positive_rate)}）",
            f"- 总召回率：{_pct(report.overall_recall)}",
            "",
            "| 错误类型 | 注入数 | 检出数 | 召回率 | 漏检示例 |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
        for error_type in ERROR_TYPES:
            stats = report.by_type[error_type]
            example = stats.missed_examples[0]["change"] if stats.missed_examples else ""
            lines.append(
                f"| {ERROR_LABELS[error_type]} | {stats.injected} | {stats.detected} | {_pct(stats.recall)} | {example} |"
            )
        if report.seed_results:
            lines += ["", "### 种子反例", "", "| 类型 | 原文 | 改坏的译文 | 检出 |", "| --- | --- | --- | :---: |"]
            for seed in report.seed_results:
                source = seed["source"].replace("\n", " ")
                corrupted = seed["corrupted"].replace("\n", " ")
                mark = "✅" if seed["detected"] else "❌"
                lines.append(f"| {ERROR_LABELS[seed['error_type']]} | {_code(source)} | {_code(corrupted)} | {mark} |")
        if report.false_positive_examples:
            lines += ["", "### 误报示例", ""]
            for example in report.false_positive_examples[:5]:
                lines.append(f"- {_code(example['source'][:80])} → {'；'.join(example['issues'])}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(prog="translator-qa-bench", description="注入错误基准（无需密钥）")
    parser.add_argument("--dataset", type=Path, default=root / "docs/evals/datasets/injection_pairs.json")
    parser.add_argument("--records", type=Path, help="结构化翻译输出的 records.json，在本地真实译文上另跑一组")
    parser.add_argument("--glossary", type=Path, help="与 --records 搭配的术语表 JSON")
    parser.add_argument("--out", type=Path, help="写出 qa_injection.md 与 qa_injection.json 的目录")
    parser.add_argument("--with-audit", action="store_true", help="检测器加上全书非文字审计（需要 pandoc）")
    args = parser.parse_args(argv)

    detector = combined_detector(rule_detector(), audit_detector()) if args.with_audit else rule_detector()
    title = "规则质检 + 非文字审计" if args.with_audit else "规则质检"
    pairs, seeds = load_dataset(json.loads(args.dataset.read_text(encoding="utf-8")))
    reports = {"自带段对": run_benchmark(pairs, detector, seed_cases=seeds)}
    command = "translator-qa-bench" + (" --with-audit" if args.with_audit else "")
    if args.records:
        glossary = json.loads(args.glossary.read_text(encoding="utf-8")) if args.glossary else {}
        records = json.loads(args.records.read_text(encoding="utf-8"))
        reports[f"本地真实译文（{args.records.name}）"] = run_benchmark(pairs_from_records(records, glossary), detector)
        command += f" --records {args.records.name}" + (f" --glossary {args.glossary.name}" if args.glossary else "")

    markdown = render_markdown(reports, command, title)
    print(markdown)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        stem = "qa_injection_audit" if args.with_audit else "qa_injection"
        (args.out / f"{stem}.md").write_text(markdown, encoding="utf-8")
        payload = {name: report.as_dict() for name, report in reports.items()}
        (args.out / f"{stem}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
