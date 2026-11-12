"""
评测命令行入口：一条命令复现两组对照评测 + RAG 门槛分析。

    translator-eval --out docs/evals/results                # 真实评测（需选手与裁判密钥）
    translator-eval --dry-run --out docs/evals/results      # 冒烟：假模型 + 假裁判跑通全流程

密钥：选手用 SILICONFLOW_API_KEY，裁判（Gemini）用 GOOGLE_API_KEY。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import date
from pathlib import Path

from ..infrastructure.llm.chat_model_factory import ChatModelFactory
from ..infrastructure.observability import UsageMetadataCollector
from ..utils.config_loader import get_config
from .judge import TranslationJudge
from .models import EvalSample
from .report import render_report
from .runner import EvaluationRunner, InstrumentedModelFactory

_PROMPT_SEPARATOR = "\n---\n"
_SOURCE_RE = re.compile(r"【待翻译文本】:\n(.*?)\n\n---\n【翻译要求】", re.S)


def load_judge_prompt(path: Path) -> str:
    """读取裁判 Prompt 模板（取 ``---`` 分隔线之后的正文）。"""
    text = path.read_text(encoding="utf-8")
    if _PROMPT_SEPARATOR in text:
        return text.split(_PROMPT_SEPARATOR, 1)[1].strip()
    return text.strip()


def load_samples(dataset_path: Path) -> list[EvalSample]:
    """按 manifest 载入评测样本（原文 + 术语表，路径相对 manifest）。"""
    manifest = json.loads(dataset_path.read_text(encoding="utf-8"))
    base = dataset_path.parent
    samples = []
    for item in manifest["samples"]:
        source_text = (base / item["source_file"]).read_text(encoding="utf-8")
        glossary = {}
        if item.get("glossary_file"):
            glossary = json.loads((base / item["glossary_file"]).read_text(encoding="utf-8"))
        samples.append(
            EvalSample(
                id=item["id"],
                kind=item["kind"],
                source_text=source_text,
                glossary=glossary,
            )
        )
    return samples


# ---- 真实评测：选手 = ChatModelFactory，裁判 = Gemini ----


def build_real_factory_builder(config):
    def builder(collector: UsageMetadataCollector):
        return InstrumentedModelFactory(ChatModelFactory(config), collector)

    return builder


def build_gemini_judge(model_name: str, prompt_template: str, collector: UsageMetadataCollector) -> TranslationJudge:
    from langchain_google_genai import ChatGoogleGenerativeAI

    model = ChatGoogleGenerativeAI(model=model_name, temperature=0.0)
    judge_runnable = model.with_config({"callbacks": [collector]})
    return TranslationJudge(judge_runnable, prompt_template)


# ---- 冒烟评测（--dry-run）：假模型 + 假裁判，机械跑通同一流水线 ----


def _dry_run_translate(prompt_value):
    text = prompt_value.to_string() if hasattr(prompt_value, "to_string") else str(prompt_value)
    match = _SOURCE_RE.search(text)
    body = match.group(1).strip() if match else text.strip()
    return f"译文：{body}"


def _dry_run_analyst(_prompt_value):
    return '{"summary": "冒烟文档", "style_notes": ["学术"], "terminology_hints": [], "section_overview": ["Root"]}'


def _dry_run_repair(_prompt_value):
    return "修正后的译文"


class _DryRunModelFactory:
    def create_translator(self):
        from langchain_core.runnables import RunnableLambda

        return RunnableLambda(_dry_run_translate)

    def create_checker(self):
        from langchain_core.runnables import RunnableLambda

        return RunnableLambda(_dry_run_repair)

    def create_analyst(self):
        from langchain_core.runnables import RunnableLambda

        return RunnableLambda(_dry_run_analyst)


def build_dry_run_factory_builder():
    def builder(collector: UsageMetadataCollector):
        return InstrumentedModelFactory(_DryRunModelFactory(), collector)

    return builder


def build_dry_run_judge(prompt_template: str) -> TranslationJudge:
    from langchain_core.runnables import RunnableLambda

    def _judge(_prompt: str) -> str:
        return '{"accuracy": 4, "fluency": 4, "terminology": 4, "rationale": "dry-run 冒烟"}'

    return TranslationJudge(RunnableLambda(_judge), prompt_template)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def main(argv: list[str] | None = None) -> int:
    root = _repo_root()
    parser = argparse.ArgumentParser(description="翻译质量评测：两组对照 + RAG 门槛分析")
    parser.add_argument("--dataset", type=Path, default=root / "docs/evals/datasets/dataset.json")
    parser.add_argument("--prompt", type=Path, default=root / "docs/evals/judge_prompt.md")
    parser.add_argument("--out", type=Path, default=root / "docs/evals/results")
    parser.add_argument("--judge-model", default="gemini-2.0-flash")
    parser.add_argument("--threshold", type=float, default=0.90)
    parser.add_argument("--estimated-cost", type=float, default=None, help="人工填写的估算 API 花费（人民币）")
    parser.add_argument(
        "--consistency-doc",
        type=Path,
        default=None,
        help="一致率分析的英文源文（较长文档才能算出跨块重复短语的一致率）；缺省则用评测数据集",
    )
    parser.add_argument(
        "--consistency-glossary",
        type=Path,
        default=None,
        help="一致率分析排除的术语表 JSON（缺省为空，即统计所有重复短语）",
    )
    parser.add_argument("--dry-run", action="store_true", help="用假模型与假裁判机械跑通流水线（无需密钥）")
    args = parser.parse_args(argv)

    config = get_config(str(root / "config" / "config.yaml"))
    prompt_template = load_judge_prompt(args.prompt)
    samples = load_samples(args.dataset)

    judge_usage = UsageMetadataCollector()
    if args.dry_run:
        judge = build_dry_run_judge(prompt_template)
        factory_builder = build_dry_run_factory_builder()
        judge_model_label = "dry-run 假裁判"
        candidate_label = "dry-run 假选手"
    else:
        judge = build_gemini_judge(args.judge_model, prompt_template, judge_usage)
        factory_builder = build_real_factory_builder(config)
        judge_model_label = args.judge_model
        candidate_label = config.model_name

    meta = {
        "date": date.today().isoformat(),
        "judge_model": judge_model_label,
        "candidate_model": candidate_label,
        "dataset": args.dataset.name,
        "command": "translator-eval --dry-run" if args.dry_run else "translator-eval",
        "note": "本次为 --dry-run 冒烟，分数与 token 均为占位，不代表真实质量。" if args.dry_run else "",
        "estimated_cost_rmb": args.estimated_cost,
    }

    consistency_source = args.consistency_doc.read_text(encoding="utf-8") if args.consistency_doc else None
    consistency_glossary = (
        json.loads(args.consistency_glossary.read_text(encoding="utf-8")) if args.consistency_glossary else {}
    )

    runner = EvaluationRunner(
        config=config,
        judge=judge,
        factory_builder=factory_builder,
        samples=samples,
        threshold=args.threshold,
        judge_usage=judge_usage,
        meta=meta,
        consistency_source=consistency_source,
        consistency_glossary=consistency_glossary,
    )
    report = asyncio.run(runner.run())

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.md").write_text(render_report(report), encoding="utf-8")
    (args.out / "report.json").write_text(report.to_json(), encoding="utf-8")

    print(f"评测完成 → {args.out / 'report.md'}")
    rate = f"{report.consistency.rate:.2%}" if report.consistency.rate is not None else "样本不足"
    print(f"一致率 {rate}（门槛 {report.consistency.threshold:.0%}）→ 决策 {report.consistency.decision}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
