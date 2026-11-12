"""
术语候选基准：把术语表的一部分条目藏起来，看候选能找回多少，与只抽大写专名的旧做法对比。不调用模型。

    translator-term-bench --source <书>.md --glossary data/glossaries/CPglossary.json --out docs/evals/results

- 按排序后的术语表条目轮流分折（默认 2 折），每折藏起一半，另一半作为已有术语表；两折合计每条都被藏过一次。
- 召回只在"书里至少出现 2 次"的条目上算（术语表里有些条目在这本书的转换稿里根本没出现，任何抽取都找不回）。
- 候选与条目匹配：大小写不敏感，允许复数、所有格等词形（Trisons 算找回 Trison）。
- 另报：候选数量、各来源命中、带歧义标记的候选、小写概念词实例、已记录误判（专名被拆成碎词）、账本提示的 token 粗估。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date
from pathlib import Path

from ..domain.index.book_index import term_pattern
from ..domain.rules.term_candidates import TermCandidate, extract_term_candidates
from ..domain.rules.term_sources import SourceConfig, corpus_text, gather_candidates
from ..pipelines.glossary.term_ledger import estimate_tokens, format_candidate, prepare_sources

# 整本书实测记录过的误判：专名短语被拆成碎词（structured_gemini.md），不应再成为候选
FRAGMENTS = ("York", "Track", "Old", "New", "Sunken")
# 记录过的错译：普通词义与专名义并存，应被标为有歧义（出现时）
KNOWN_AMBIGUOUS = ("Mission", "Gates", "Centauri")


def _found(entry: str, candidates: list[TermCandidate]) -> TermCandidate | None:
    pattern = term_pattern(entry)
    return next((c for c in candidates if c.term.lower() == entry.lower() or pattern.fullmatch(c.term)), None)


def _ledger_prompt_tokens(candidates: list[TermCandidate], batch_size: int, legacy: bool) -> tuple[int, int]:
    """账本提示的 token 粗估（只算候选部分与固定说明，不含摘要与已有术语）：返回 (调用次数, token)。"""
    from ..pipelines.translate.prompt_builder import TranslationPromptBuilder
    from ..utils.config_loader import get_config

    template = TranslationPromptBuilder(get_config()).build_term_ledger_prompt()
    calls, tokens = 0, 0
    size = len(candidates) if legacy else batch_size
    for start in range(0, len(candidates), max(1, size)):
        batch = candidates[start : start + size]
        if legacy:
            lines = "\n".join(f"- {c.term}（{c.count} 次）：…{c.context}…" for c in batch)
        else:
            lines = "\n".join(format_candidate(c) for c in batch)
        tokens += estimate_tokens(template.format(summary="（无）", existing_terms="（无）", candidates=lines))
        calls += 1
    return calls, tokens


def run(text: str, glossary: dict[str, str], chunks=None, folds: int = 2, config: SourceConfig | None = None) -> dict:
    config = config or SourceConfig()
    index, cues = prepare_sources(text, chunks)
    corpus = corpus_text(index)
    occurrences = {entry: len(term_pattern(entry).findall(corpus)) for entry in glossary}
    reachable = {entry for entry, count in occurrences.items() if count >= 2}
    entries = sorted(glossary)

    report: dict = {
        "glossary_entries": len(glossary),
        "reachable_entries": len(reachable),
        "folds": folds,
        "segments": len(index),
        "structure_cues": sum(cues.values()) if cues else 0,
        "methods": {},
    }
    found_by: dict[str, dict[str, str]] = {"baseline": {}, "multi_source": {}}
    counts: dict[str, list[int]] = {"baseline": [], "multi_source": []}
    source_hits: Counter = Counter()
    for fold in range(folds):
        hidden = [entry for i, entry in enumerate(entries) if i % folds == fold]
        given = {entry: glossary[entry] for entry in entries if entry not in hidden}
        baseline = extract_term_candidates(text, given, max_terms=120)
        multi = gather_candidates(index, given, text=text, cues=cues, config=config, with_renderings=False)
        counts["baseline"].append(len(baseline))
        counts["multi_source"].append(len(multi))
        for entry in hidden:
            if hit := _found(entry, baseline):
                found_by["baseline"][entry] = hit.term
            if hit := _found(entry, multi):
                found_by["multi_source"][entry] = hit.term
                source_hits.update(hit.sources)

    for method in ("baseline", "multi_source"):
        found = found_by[method]
        report["methods"][method] = {
            "candidates_per_fold": counts[method],
            "found": len(found),
            "recall_all": round(len(found) / len(glossary), 4) if glossary else None,
            "found_reachable": len(set(found) & reachable),
            "recall_reachable": round(len(set(found) & reachable) / len(reachable), 4) if reachable else None,
            "found_entries": dict(sorted(found.items())),
        }
    report["missed_reachable"] = sorted(reachable - set(found_by["multi_source"]))
    report["newly_found"] = sorted(set(found_by["multi_source"]) - set(found_by["baseline"]))
    report["lost"] = sorted(set(found_by["baseline"]) - set(found_by["multi_source"]))
    report["hidden_hits_by_source"] = dict(source_hits)

    # 正式运行（整份术语表都给出）时的候选与成本
    baseline_full = extract_term_candidates(text, glossary, max_terms=120)
    full = gather_candidates(index, glossary, text=text, cues=cues, config=config, with_renderings=False)
    terms = {c.term for c in full} | {c.term for c in baseline_full}
    base_calls, base_tokens = _ledger_prompt_tokens(baseline_full, 120, legacy=True)
    calls, tokens = _ledger_prompt_tokens(full, 60, legacy=False)
    report["full_run"] = {
        "baseline_candidates": len(baseline_full),
        "candidates": len(full),
        "by_source": dict(Counter(source for c in full for source in c.sources)),
        "ambiguous": len([c for c in full if c.ambiguous]),
        "ambiguity_signals": dict(Counter(signal for c in full for signal in c.ambiguity)),
        "mean_contexts": round(sum(len(c.contexts) for c in full) / len(full), 2) if full else 0,
        "baseline_ledger": {"calls": base_calls, "estimated_prompt_tokens": base_tokens},
        "ledger": {"calls": calls, "estimated_prompt_tokens": tokens},
        "lowercase_concepts": [
            {"term": c.term, "count": c.count, "keyness": c.keyness}
            for c in full
            if c.term[:1].islower() and "keyness" in c.sources
        ][:25],
        "fragments_in_candidates": sorted(set(FRAGMENTS) & terms),
        "known_ambiguous": {
            c.term: c.ambiguity for c in full if c.term in KNOWN_AMBIGUOUS or c.term.lower() in {"mission", "gates"}
        },
    }
    return report


def render_markdown(report: dict, command: str) -> str:
    base, multi = report["methods"]["baseline"], report["methods"]["multi_source"]
    full = report["full_run"]

    def pct(value):
        return "—" if value is None else f"{value:.0%}"

    lines = [
        "# 术语候选基准：藏起术语表条目后的召回",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        f"- 复现命令：`{command}`",
        f"- 术语表 {report['glossary_entries']} 条，其中在本书出现 ≥ 2 次的 {report['reachable_entries']} 条；"
        f"{report['folds']} 折轮流藏起，每条都被藏过一次。全书 {report['segments']} 段。",
        "",
        "| 方法 | 每折候选数 | 找回（全部条目） | 召回（全部） | 找回（出现 ≥ 2 次） | 召回（出现 ≥ 2 次） |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
        f"| 现状：只抽大写专名（上限 120） | {base['candidates_per_fold']} | {base['found']} | {pct(base['recall_all'])} "
        f"| {base['found_reachable']} | {pct(base['recall_reachable'])} |",
        f"| 多路候选（专名 + 关键度 + 结构） | {multi['candidates_per_fold']} "
        f"| {multi['found']} | {pct(multi['recall_all'])} | {multi['found_reachable']} | {pct(multi['recall_reachable'])} |",
        "",
        f"- 新找回：{', '.join(report['newly_found']) or '无'}",
        f"- 现状找到而多路丢失：{', '.join(report['lost']) or '无'}",
        f"- 仍未找回（出现 ≥ 2 次）：{', '.join(report['missed_reachable']) or '无'}",
        f"- 找回条目的来源（一个候选可有多个来源）：{report['hidden_hits_by_source']}",
        "",
        "## 正式运行（整份术语表都给出）",
        "",
        f"- 候选：现状 {full['baseline_candidates']} 条 → 多路 {full['candidates']} 条，来源 {full['by_source']}",
        f"- 带歧义标记：{full['ambiguous']} 条，信号分布 {full['ambiguity_signals']}",
        f"- 每个候选平均上下文：{full['mean_contexts']} 处",
        f"- 账本提示（粗估，不含摘要与已有术语）：现状 {full['baseline_ledger']['calls']} 次调用 / "
        f"≈{full['baseline_ledger']['estimated_prompt_tokens']} token → 多路 {full['ledger']['calls']} 次 / "
        f"≈{full['ledger']['estimated_prompt_tokens']} token",
        f"- 已记录误判（专名拆成碎词）出现在候选中：{', '.join(full['fragments_in_candidates']) or '无'}",
        f"- 已知易错词的歧义标记：{full['known_ambiguous'] or '（未成为候选）'}",
        "",
        "### 小写概念词实例（关键度一路）",
        "",
        ", ".join(f"{item['term']}（{item['count']}）" for item in full["lowercase_concepts"]) or "无",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="translator-term-bench", description="术语候选召回基准（无需密钥）")
    parser.add_argument("--source", type=Path, required=True, help="书的 Markdown 源文")
    parser.add_argument("--glossary", type=Path, required=True, help="作为标准答案的术语表 JSON")
    parser.add_argument("--folds", type=int, default=2)
    parser.add_argument("--max-keyness", type=int, default=SourceConfig().max_keyness)
    parser.add_argument("--max-structure", type=int, default=SourceConfig().max_structure)
    parser.add_argument("--plain", action="store_true", help="不用 Document IR 分块，按空行拆段（模拟非结构化模式）")
    parser.add_argument("--out", type=Path, help="写出 term_candidates.md / .json 的目录")
    args = parser.parse_args(argv)

    text = args.source.read_text(encoding="utf-8")
    glossary = json.loads(args.glossary.read_text(encoding="utf-8"))
    chunks = None
    if not args.plain:
        from ..infrastructure.document import pandoc

        if pandoc.pandoc_available():
            from ..pipelines.structure.planner import StructuredDocumentPlanner
            from ..utils.config_loader import get_config

            chunks = StructuredDocumentPlanner.from_config(get_config()).plan(text).chunks
    config = SourceConfig(max_keyness=args.max_keyness, max_structure=args.max_structure)
    report = run(text, glossary, chunks, args.folds, config)
    command = f"translator-term-bench --source {args.source.name} --glossary {args.glossary.name}"
    if (args.max_keyness, args.max_structure) != (SourceConfig().max_keyness, SourceConfig().max_structure):
        command += f" --max-keyness {args.max_keyness} --max-structure {args.max_structure}"
    markdown = render_markdown(report, command + (" --plain" if args.plain else ""))
    print(markdown)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        stem = "term_candidates" + ("_plain" if args.plain else "")
        (args.out / f"{stem}.md").write_text(markdown, encoding="utf-8")
        (args.out / f"{stem}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
