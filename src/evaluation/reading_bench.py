"""
阅读遍消融（#17）：开 / 关阅读遍时，被藏起的术语表条目的召回率与 token 成本。

    translator-reading-bench --source <书>.md --glossary data/glossaries/CPglossary.json            # 只算成本，不调用模型
    translator-reading-bench --source <书>.md --glossary ... --model --config config/<配置>.yaml      # 调用分析员模型，另算召回

- 不加 --model 时用一个返回空笔记的假模型跑完整个阅读遍：分章、累计状态、提示长度都是真实的，
  只有输出是空的，所以提示 token 是实际会发送的量，输出 token 按每章笔记约 OUTPUT_TOKENS_PER_CHAPTER 粗估；
  召回需要模型真的读书，这一档不报召回。
- 加 --model 时阅读遍只跑一次（笔记与术语表无关），之后按 term_bench 同样的分折，比较
  "旧版（只抽专名）"、"旧版 + 阅读遍"、"多路"、"多路 + 阅读遍" 四组的召回。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path

from langchain_core.runnables import RunnableLambda

from ..domain.rules.term_sources import PROPER, SourceConfig, gather_candidates
from ..pipelines.glossary.reading_pass import ReadingPass
from ..pipelines.glossary.term_ledger import estimate_tokens, prepare_sources
from .term_bench import _found

OUTPUT_TOKENS_PER_CHAPTER = 350  # 一份章节笔记（摘要、全书概括、≤8 个概念、≤5 个特殊用词）的输出量粗估

_EMPTY_NOTE = '{"summary": "", "book_so_far": "", "concepts": [], "special_words": []}'


def _full_note(prompt_value) -> str:
    """每章都写满上限的笔记（8 个新概念、5 个特殊用词、摘要写满）：累计状态与提示长度的上界估计。"""
    import re

    text = prompt_value.to_string() if hasattr(prompt_value, "to_string") else str(prompt_value)
    match = re.search(r"【当前章节】: 第 (\d+) 章", text)
    n = match.group(1) if match else "0"
    return json.dumps(
        {
            "summary": "本章摘要" * 30,
            "book_so_far": "全书概括" * 50,
            "concepts": [
                {"term": f"concept {n}-{i}", "definition": "作者赋予的含义" * 12, "where": "定义所在的小节"}
                for i in range(8)
            ],
            "special_words": [{"word": f"word{n}{i}", "meaning": "特殊含义" * 10} for i in range(5)],
        },
        ensure_ascii=False,
    )


class _Config:
    def __init__(self, base=None):
        self.base = base

    def get(self, key, default=None):
        value = self.base.get(key, None) if self.base is not None else None
        return default if value is None else value


def _plan(text: str, plain: bool):
    if plain:
        return None
    from ..infrastructure.document import pandoc

    if not pandoc.pandoc_available():
        return None
    from ..pipelines.structure.planner import StructuredDocumentPlanner
    from ..utils.config_loader import get_config

    return StructuredDocumentPlanner.from_config(get_config()).plan(text).chunks


def ablation(text: str, glossary: dict[str, str], concepts: list[tuple[str, int]], chunks=None, folds: int = 2) -> dict:
    """四组召回：旧版 / 旧版 + 阅读遍 / 多路 / 多路 + 阅读遍。只在"书里出现 ≥ 2 次"的条目上算。"""
    from ..domain.index.book_index import term_pattern
    from ..domain.rules.term_sources import corpus_text

    index, cues = prepare_sources(text, chunks)
    corpus = corpus_text(index)
    reachable = {entry for entry in glossary if len(term_pattern(entry).findall(corpus)) >= 2}
    entries = sorted(glossary)
    legacy = SourceConfig(sources=(PROPER,), max_terms=120, contexts=1)
    multi = SourceConfig()
    groups = {
        "legacy": (legacy, None),
        "legacy+reading": (legacy, concepts),
        "multi": (multi, None),
        "multi+reading": (multi, concepts),
    }
    found: dict[str, set[str]] = {name: set() for name in groups}
    sizes: dict[str, list[int]] = {name: [] for name in groups}
    for fold in range(folds):
        hidden = [entry for i, entry in enumerate(entries) if i % folds == fold]
        given = {entry: glossary[entry] for entry in entries if entry not in hidden}
        for name, (config, reading) in groups.items():
            candidates = gather_candidates(
                index, given, text=text, cues=cues, config=config, with_renderings=False, reading=reading
            )
            sizes[name].append(len(candidates))
            found[name] |= {entry for entry in hidden if entry in reachable and _found(entry, candidates)}
    return {
        "reachable_entries": len(reachable),
        "groups": {
            name: {
                "candidates_per_fold": sizes[name],
                "found_reachable": len(found[name]),
                "recall_reachable": round(len(found[name]) / len(reachable), 4) if reachable else None,
            }
            for name in groups
        },
        "added_by_reading_over_legacy": sorted(found["legacy+reading"] - found["legacy"]),
        "added_by_reading_over_multi": sorted(found["multi+reading"] - found["multi"]),
    }


def run(
    text: str, glossary: dict[str, str], llm=None, config=None, chunks=None, folds: int = 2, full_notes: bool = False
) -> dict:
    model_used = llm is not None
    llm = llm or RunnableLambda(_full_note if full_notes else (lambda _prompt: _EMPTY_NOTE))
    result = asyncio.run(ReadingPass(llm, _Config(config)).run(text, chunks))
    usage = result.usage.as_dict()
    estimated_output = usage["calls"] * OUTPUT_TOKENS_PER_CHAPTER
    report = {
        "model_used": model_used,
        "full_notes": full_notes and not model_used,
        "chapters": usage["chapters"],
        "usage": usage,
        "estimated_output_tokens": estimated_output,
        "estimated_total_tokens": usage["estimated_prompt_tokens"] + estimated_output,
        "concepts": len(result.concepts),
        "profile": {
            "summary": result.profile.summary,
            "overview_lines": len(result.profile.section_overview),
            "term_hints": len(result.profile.terminology_hints),
            "prompt_chars": len(result.profile.to_prompt_text()),
            "prompt_tokens": estimate_tokens(result.profile.to_prompt_text()),
            "chunks": len(chunks) if chunks else None,
        },
    }
    if model_used:
        report["ablation"] = ablation(text, glossary, result.concepts, chunks, folds)
        report["concept_examples"] = [term for term, _ in result.concepts[:40]]
    return report


def render_markdown(report: dict, command: str, translation_tokens: int | None) -> str:
    usage = report["usage"]
    total = report["estimated_total_tokens"]
    ratio = (
        f"（约为纯翻译 {translation_tokens} token 的 {total / translation_tokens:.0%}）" if translation_tokens else ""
    )
    lines = [
        "# 阅读遍消融",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        f"- 复现命令：`{command}`",
        f"- 章节（阅读单位）：{report['chapters']}；模型调用 {usage['calls']} 次",
        f"- 提示 token：≈{usage['estimated_prompt_tokens']}（按字符粗估；服务端回报 {usage['reported_tokens'] or '无'}）",
        f"- 输出 token：≈{report['estimated_output_tokens']}（按每章 {OUTPUT_TOKENS_PER_CHAPTER} 粗估）",
        f"- 合计：≈{total}{ratio}",
        f"- 累计状态最大 {usage['max_state_chars']} 字符（上限见 multi_agent.reading_pass.max_state_chars）",
        f"- 画像：{report['profile']['overview_lines']} 行章节概览、{report['profile']['term_hints']} 条术语提示，"
        f"提示文本 {report['profile']['prompt_chars']} 字符 ≈ {report['profile']['prompt_tokens']} token，"
        f"随每块发送（{report['profile']['chunks'] or '?'} 块）；上限见 multi_agent.reading_pass.profile_max_chars",
    ]
    if not report["model_used"]:
        kind = "每章笔记都写满上限（成本上界）" if report.get("full_notes") else "笔记为空（成本下界）"
        lines += ["", f"> 未调用模型：{kind}，只测了成本。召回要加 `--model` 用真实模型跑。"]
        return "\n".join(lines) + "\n"
    ab = report["ablation"]
    names = {
        "legacy": "旧版：只抽专名",
        "legacy+reading": "旧版 + 阅读遍",
        "multi": "多路（专名 + 关键度 + 结构）",
        "multi+reading": "多路 + 阅读遍",
    }
    lines += [
        "",
        f"## 召回（书里出现 ≥ 2 次的 {ab['reachable_entries']} 条）",
        "",
        "| 方法 | 每折候选数 | 找回 | 召回 |",
        "| --- | --- | ---: | ---: |",
    ]
    for key, label in names.items():
        group = ab["groups"][key]
        lines.append(
            f"| {label} | {group['candidates_per_fold']} | {group['found_reachable']} | {group['recall_reachable']:.0%} |"
        )
    lines += [
        "",
        f"- 阅读遍在旧版之上新找回：{', '.join(ab['added_by_reading_over_legacy']) or '无'}",
        f"- 阅读遍在多路之上新找回：{', '.join(ab['added_by_reading_over_multi']) or '无'}",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="translator-reading-bench", description="阅读遍消融：成本与术语召回")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--glossary", type=Path, required=True)
    parser.add_argument("--model", action="store_true", help="调用分析员模型真实阅读（需要密钥），另算召回")
    parser.add_argument("--config", type=Path, help="与 --model 搭配的配置文件")
    parser.add_argument("--translation-tokens", type=int, help="同一本书纯翻译的总 token，用来折算比例")
    parser.add_argument("--full-notes", action="store_true", help="不调模型时让假笔记写满上限，估计成本上界")
    parser.add_argument("--folds", type=int, default=2)
    parser.add_argument("--plain", action="store_true", help="不用 Document IR 分块")
    parser.add_argument("--out", type=Path, help="写出 reading_pass.md / .json 的目录")
    args = parser.parse_args(argv)

    text = args.source.read_text(encoding="utf-8")
    glossary = json.loads(args.glossary.read_text(encoding="utf-8"))
    llm, config = None, None
    if args.model:
        from ..infrastructure.llm.chat_model_factory import ChatModelFactory
        from ..utils.config_loader import get_config

        config = get_config(args.config) if args.config else get_config()
        llm = ChatModelFactory(config).create_analyst()
    report = run(text, glossary, llm, config, _plan(text, args.plain), args.folds, args.full_notes)
    command = f"translator-reading-bench --source {args.source.name} --glossary {args.glossary.name}"
    if args.model:
        command += " --model" + (f" --config {args.config}" if args.config else "")
    if args.full_notes and not args.model:
        command += " --full-notes"
    if args.translation_tokens:
        command += f" --translation-tokens {args.translation_tokens}"
    markdown = render_markdown(report, command + (" --plain" if args.plain else ""), args.translation_tokens)
    print(markdown)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        stem = "reading_pass" + ("_full_notes" if args.full_notes and not args.model else "")
        (args.out / f"{stem}.md").write_text(markdown, encoding="utf-8")
        (args.out / f"{stem}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
