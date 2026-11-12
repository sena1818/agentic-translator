"""
术语 agent 基准：在有歧义的候选上对比"一次性账本调用"与"术语 agent 查证"。

    translator-term-agent-bench --source <书>.md --glossary data/glossaries/CPglossary.json --config <配置>.yaml
    translator-term-agent-bench --source data/temp/ccru/Ccru.md --book Ccru --config <配置>.yaml
    translator-term-agent-bench --source <书>.md --glossary ... --dry-run    # 不调用模型，只列出评测条目

评测条目 = 被确定性信号标为有歧义、且有答案可对照的候选：
- 被藏起的术语表条目（按 term_bench 同样的方式分折，每折藏一半）：答案是该条目的译名；
- 已记录的误判（docs/evals/datasets/term_misjudgments.json）：只知道错的译名。

"强制译名"指会作为全书统一译名并入术语表的结果：一次性调用里 keep=true 的译名；agent 里单一译名的 decided。
误判 = 强制译名与答案不符（或落在已记录的错误译名里）。agent 的多义项、普通词、转交人工都不强制，
不算误判，但单独报告转交比例，避免把"全部转交"当成零误判。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..domain.index.book_index import BookIndex, term_pattern
from ..domain.rules.term_candidates import TermCandidate
from ..domain.rules.term_sources import SourceConfig, corpus_text, gather_candidates
from ..infrastructure.agent.model import estimate_tokens
from ..pipelines.glossary.term_agent import TermAgent, TermAgentSettings
from ..pipelines.glossary.term_ledger import TermLedgerBuilder, format_candidate, prepare_sources

FULL_SOURCES = SourceConfig(sources=("proper", "keyness", "structure"), max_terms=240, contexts=3)


@dataclass
class BenchItem:
    term: str
    candidate: TermCandidate
    gold: str = ""  # 术语表答案（首选译名）
    wrong: tuple[str, ...] = ()  # 已记录的错误译名
    known: dict[str, str] = field(default_factory=dict)  # 这一折里可见的术语表


@dataclass
class MethodResult:
    rendering: str | None  # 强制译名；None 表示不强制
    status: str  # enforced | multi_sense | not_term | handoff | dropped
    steps: int = 0
    tokens: int = 0


def preferred(value: str) -> str:
    return value.split(" (", 1)[0].strip()


def misjudged(item: BenchItem, rendering: str | None) -> bool:
    if not rendering:
        return False
    rendering = preferred(rendering)
    if item.wrong:
        return any(wrong == rendering or (len(wrong) >= 2 and wrong in rendering) for wrong in item.wrong)
    gold = preferred(item.gold)
    return not (rendering == gold or (min(len(rendering), len(gold)) >= 2 and (gold in rendering or rendering in gold)))


def collect_items(
    text: str,
    glossary: dict[str, str],
    misjudgments: Sequence[dict],
    chunks=None,
    folds: int = 2,
    config: SourceConfig = FULL_SOURCES,
) -> tuple[BookIndex, list[BenchItem]]:
    index, cues = prepare_sources(text, chunks)
    corpus = corpus_text(index)
    reachable = sorted(entry for entry in glossary if len(term_pattern(entry).findall(corpus)) >= 2)
    items: dict[str, BenchItem] = {}
    for fold in range(max(1, folds) if reachable else 1):
        hidden = {entry for i, entry in enumerate(reachable) if i % max(1, folds) == fold}
        known = {entry: value for entry, value in glossary.items() if entry not in hidden}
        for candidate in gather_candidates(index, known, text=text, cues=cues, config=config):
            if not candidate.ambiguous or candidate.term.casefold() in {i.casefold() for i in items}:
                continue
            gold = next((glossary[e] for e in hidden if e.casefold() == candidate.term.casefold()), "")
            wrong = next(
                (tuple(m["wrong"]) for m in misjudgments if m["term"].casefold() == candidate.term.casefold()), ()
            )
            if gold or wrong:
                items[candidate.term] = BenchItem(candidate.term, candidate, gold, wrong, known)
    return index, list(items.values())


async def run_one_shot(items: Sequence[BenchItem], llm, prompt_builder, summary: str = "") -> dict[str, MethodResult]:
    """与账本同一个 Prompt，把评测条目一次性交给模型（每个条目带它那一折可见的术语表）。"""
    from ..pipelines.translate.langchain_compat import StrOutputParser

    prompt = prompt_builder.build_term_ledger_prompt()
    results: dict[str, MethodResult] = {}
    for item in items:
        payload = {
            "summary": summary or "（无）",
            "existing_terms": "\n".join(f"- {en}: {zh}" for en, zh in list(item.known.items())[:40]) or "（无）",
            "candidates": format_candidate(item.candidate),
        }
        raw = await (prompt | llm | StrOutputParser()).ainvoke(payload)
        tokens = estimate_tokens(prompt.format(**payload)) + estimate_tokens(raw)
        parsed = TermLedgerBuilder.parse(raw, {item.term})
        rendering = parsed.get(item.term)
        results[item.term] = MethodResult(rendering, "enforced" if rendering else "dropped", 1, tokens)
    return results


async def run_agent(items: Sequence[BenchItem], index: BookIndex, model, settings: TermAgentSettings) -> dict:
    results: dict[str, MethodResult] = {}
    for item in items:
        report = await TermAgent(model, settings).run([item.candidate], index, item.known, {})
        decision = report.decisions[0]
        rendering = decision.single_rendering
        status = (
            "enforced"
            if rendering
            else {"decided": "multi_sense", "not_term": "not_term", "handoff": "handoff"}[decision.status]
        )
        results[item.term] = MethodResult(rendering, status, decision.steps, decision.tokens)
    return results


def score(items: Sequence[BenchItem], results: dict[str, MethodResult]) -> dict:
    count = len(items)
    rows = [results[item.term] for item in items]
    wrong = [item.term for item in items if misjudged(item, results[item.term].rendering)]
    statuses: dict[str, int] = {}
    for row in rows:
        statuses[row.status] = statuses.get(row.status, 0) + 1
    return {
        "items": count,
        "misjudged": len(wrong),
        "misjudgment_rate": round(len(wrong) / count, 4) if count else None,
        "misjudged_terms": wrong,
        "statuses": statuses,
        "handoff_rate": round(statuses.get("handoff", 0) / count, 4) if count else None,
        "mean_steps": round(sum(r.steps for r in rows) / count, 2) if count else None,
        "tokens": sum(r.tokens for r in rows),
    }


def render(report: dict) -> str:
    lines = [
        "# 术语 agent 基准：一次性调用 vs agent 查证",
        "",
        f"- 生成日期：{report['date']}",
        f"- 书：{report['book']}；评测条目 {report['items']} 条（藏起的术语表条目 {report['gold_items']}，已记录误判 {report['misjudgment_items']}）",
        f"- 模型：{report.get('model', '未调用（--dry-run）')}",
        "",
    ]
    if "one_shot" not in report:
        lines += ["本次为 --dry-run，没有调用模型；下面只列出评测条目。", ""]
    else:
        lines += [
            "| 方法 | 误判 | 误判率 | 转交人工 | 平均步数 | token |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for name, label in (("one_shot", "一次性调用"), ("agent", "术语 agent")):
            row = report[name]
            lines.append(
                f"| {label} | {row['misjudged']} | {row['misjudgment_rate']:.0%} | "
                f"{row['handoff_rate']:.0%} | {row['mean_steps']} | {row['tokens']} |"
            )
        lines += [""]
    lines += ["| 条目 | 歧义信号 | 答案 / 已知错误 |", "| --- | --- | --- |"]
    for item in report["item_list"]:
        lines.append(
            f"| {item['term']} | {'、'.join(item['ambiguity'])} | {item['gold'] or '≠ ' + '/'.join(item['wrong'])} |"
        )
    return "\n".join(lines) + "\n"


def build_report(book: str, items: Sequence[BenchItem]) -> dict:
    return {
        "date": date.today().isoformat(),
        "book": book,
        "items": len(items),
        "gold_items": sum(1 for item in items if item.gold),
        "misjudgment_items": sum(1 for item in items if item.wrong),
        "item_list": [
            {"term": i.term, "ambiguity": i.candidate.ambiguity, "gold": i.gold, "wrong": list(i.wrong)} for i in items
        ],
    }


def main(argv: list[str] | None = None, model_builder: Callable | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(prog="translator-term-agent-bench", description="术语 agent 对比基准")
    parser.add_argument("--source", type=Path, required=True, help="书的 Markdown 源文")
    parser.add_argument("--glossary", type=Path, help="完整术语表 JSON（藏起一半作为答案）")
    parser.add_argument("--book", default="", help="书名，用于匹配已记录误判（默认取文件名）")
    parser.add_argument("--misjudgments", type=Path, default=root / "docs/evals/datasets/term_misjudgments.json")
    parser.add_argument("--config", type=Path, help="模型配置；不给则等同 --dry-run")
    parser.add_argument("--folds", type=int, default=2)
    parser.add_argument("--max-items", type=int, default=40)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", type=Path, help="写出 term_agent_bench.md / .json 的目录")
    args = parser.parse_args(argv)

    text = args.source.read_text(encoding="utf-8")
    glossary = json.loads(args.glossary.read_text(encoding="utf-8")) if args.glossary else {}
    book = args.book or args.source.stem.split("_")[-1]
    records = json.loads(args.misjudgments.read_text(encoding="utf-8")).get("items", [])
    misjudgments = [m for m in records if m["book"].casefold() == book.casefold() and m["term"] in text]
    index, items = collect_items(text, glossary, misjudgments, folds=args.folds)
    items = items[: args.max_items]
    report = build_report(book, items)

    if not args.dry_run and (args.config is not None or model_builder is not None):
        from ..infrastructure.agent import RunnableChatModel
        from ..pipelines.translate.prompt_builder import TranslationPromptBuilder
        from ..utils.config_loader import Config

        config = Config(str(args.config)) if args.config else None
        if model_builder is not None:
            llm, model_name = model_builder(config)
        else:
            from ..infrastructure.llm.chat_model_factory import ChatModelFactory

            llm, model_name = ChatModelFactory(config).create_analyst(), config.get("api.model")
        prompt_builder = TranslationPromptBuilder(config) if config else TranslationPromptBuilder(_default_config())
        settings = TermAgentSettings.from_config(config) if config else TermAgentSettings()
        settings = TermAgentSettings(True, len(items) or 1, settings.budget)
        one_shot = asyncio.run(run_one_shot(items, llm, prompt_builder))
        agent = asyncio.run(run_agent(items, index, RunnableChatModel(llm), settings))
        report.update(model=model_name, one_shot=score(items, one_shot), agent=score(items, agent))

    markdown = render(report)
    print(markdown)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "term_agent_bench.md").write_text(markdown, encoding="utf-8")
        (args.out / "term_agent_bench.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


def _default_config():
    from ..utils.config_loader import get_config

    return get_config()


if __name__ == "__main__":
    raise SystemExit(main())
