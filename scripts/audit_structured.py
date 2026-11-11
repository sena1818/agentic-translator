"""对一次已完成的结构化翻译重跑全书非文字审计（不调用模型）。

用法:
    python scripts/audit_structured.py <源文件.md> <X.records.json> [--config config/config.yaml]
        [--assets <图片目录> ...] [--json <输出.json>] [--report <输出.md>]
"""

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.getcwd())

from src.domain.document.nontext_audit import AuditReport  # noqa: E402
from src.pipelines.structure.audit import image_checker, run_audit  # noqa: E402
from src.pipelines.structure.exporter import StructuredExporter  # noqa: E402
from src.utils.config_loader import get_config  # noqa: E402

ORIGINS = {"translation": "翻译引入", "source": "原文本身", "notice": "仅提示"}


def render(report: AuditReport, source: Path, records: Path) -> str:
    summary = report.summary
    lines = [
        f"# 非文字审计：{source.stem}",
        "",
        f"- 源文件：`{source.name}`；翻译记录：`{records.name}`",
        "- 审计为确定性程序，不调用模型。",
        "",
        "## 原文清点",
        "",
        "| 对象 | 数量 |",
        "| --- | ---: |",
        *[f"| {name} | {count} |" for name, count in summary["inventory"].items()],
        "",
        "## 发现",
        "",
        "| 来源 | 类别 | 数量 |",
        "| --- | --- | ---: |",
    ]
    for origin, kinds in summary["by_origin"].items():
        for kind, count in sorted(kinds.items()):
            lines.append(f"| {ORIGINS.get(origin, origin)} | {kind} | {count} |")
    by_severity = Counter((f.origin, f.severity) for f in report.findings)
    lines += ["", "按严重度：" + "，".join(f"{ORIGINS.get(o, o)}/{s} {n}" for (o, s), n in sorted(by_severity.items()))]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("records", type=Path)
    parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    parser.add_argument("--assets", type=Path, nargs="*", default=[])
    parser.add_argument("--json", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    exporter = StructuredExporter.from_config(get_config(args.config))
    document = exporter.assemble(
        args.source.read_text(encoding="utf-8"), json.loads(args.records.read_text(encoding="utf-8"))
    )
    if document is None:
        sys.exit("翻译记录与结构化规划不一致（配置的分块参数与翻译时不同？）")
    report = run_audit(document, image_checker(args.assets) if args.assets else None)
    print(json.dumps(report.summary, ensure_ascii=False, indent=2))
    if args.json:
        args.json.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    if args.report:
        args.report.write_text(render(report, args.source, args.records), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
