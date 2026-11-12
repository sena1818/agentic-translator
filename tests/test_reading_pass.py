"""阅读遍：状态有上界、画像带出处、续跑不重复花费、关闭时行为不变、概念并入术语候选。全部用假模型。"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from langchain_core.runnables import RunnableLambda

from src.domain.index.book_index import BookIndex, IndexedSegment
from src.domain.rules.reading_pass import (
    Chapter,
    ChapterNote,
    ConceptNote,
    ReadingLimits,
    ReadingState,
    SpecialWord,
    build_profile,
    concept_terms,
    parse_note,
    split_chapters,
)
from src.domain.rules.term_sources import READING, SourceConfig, gather_candidates, paragraph_index
from src.pipelines.glossary.reading_pass import ReadingPass


def _book(chapters: int = 5, paragraphs: int = 3) -> str:
    parts = []
    for c in range(1, chapters + 1):
        parts.append(f"# Chapter {c}: The Plateau {c}")
        for p in range(paragraphs):
            parts.append(
                f"In chapter {c} the xenochronic drift returns as paragraph {p}. "
                "Here the author keeps describing the desert, the oil and the hole complex at length. " * 4
            )
    return "\n\n".join(parts) + "\n"


def _index(text: str) -> BookIndex:
    return paragraph_index(text)


# --------------------------------------------------------------------------- #
# 纯逻辑
# --------------------------------------------------------------------------- #


def test_chapters_merge_short_runs_and_split_long_ones():
    segments = [IndexedSegment(i, 0, "x" * 500, section_path=(f"S{i // 2}",)) for i in range(10)]
    segments += [IndexedSegment(10 + i, 0, "y" * 5000, section_path=("Long",)) for i in range(6)]
    limits = ReadingLimits(min_chapter_chars=1500, max_chapter_chars=12000)
    chapters = split_chapters(BookIndex(segments), limits)
    assert all(len(ch.text) <= limits.max_chapter_chars + 100 for ch in chapters)
    covered = [pos for ch in chapters for pos in range(ch.positions[0], ch.positions[1] + 1)]
    assert covered == list(range(16))  # 每段恰好读一次，顺序不变
    long_parts = [ch for ch in chapters if ch.title.startswith("Long")]
    assert len(long_parts) == 3 and "部分" in long_parts[0].title
    assert [ch.number for ch in chapters] == list(range(1, len(chapters) + 1))


def test_state_stays_bounded_over_a_whole_book():
    limits = ReadingLimits()
    state = ReadingState(limits)
    sizes = []
    for n in range(1, 301):
        note = ChapterNote(
            chapter=n,
            title=f"A rather long chapter title number {n}",
            summary="本章摘要" * 40,
            book_so_far="全书至今" * 100,
            concepts=[ConceptNote(f"concept{n}x{i}", "定义" * 40, "where") for i in range(8)],
            special_words=[SpecialWord(f"word{n}x{i}", "含义" * 30) for i in range(5)],
        )
        state.update(note)
        sizes.append(len(state.render()))
    assert max(sizes) <= limits.max_state_chars
    assert sizes[-1] <= sizes[49] + 50  # 读到第 50 章后不再增长
    assert len(state.summaries) <= limits.state_recent_chapters + limits.state_older_chapters
    assert len(state.concepts) <= limits.state_concepts * 2


def test_repeated_concepts_rank_first_in_state():
    state = ReadingState(ReadingLimits(state_concepts=3))
    for n in range(1, 8):
        concepts = [ConceptNote("Numogram", "数字图")] + [ConceptNote(f"once{n}", "只出现一次")]
        state.update(ChapterNote(n, f"T{n}", summary="s", concepts=concepts))
    assert "Numogram" in state.render() and "（第 1 章《T1》）" in state.render()


def test_profile_entries_carry_chapter_provenance():
    notes = [
        ChapterNote(
            1,
            "Oil",
            summary="石油作为叙事行动者",
            book_so_far="讲石油",
            concepts=[ConceptNote("Petropolitics", "石油政治", "开篇")],
        ),
        ChapterNote(
            2, "Dust", summary="尘埃与沙漠", book_so_far="讲石油与尘埃", concepts=[ConceptNote("Dustism", "尘埃主义")]
        ),
        ChapterNote(3, "Bad", valid=False),
    ]
    profile = build_profile(notes)
    assert profile.summary == "讲石油与尘埃"
    assert [line.split("：")[0] for line in profile.section_overview] == ["第 1 章《Oil》", "第 2 章《Dust》"]
    assert profile.terminology_hints[0] == "Petropolitics：石油政治（首见第 1 章《Oil》）"
    assert all("首见第" in hint for hint in profile.terminology_hints)


def test_profile_is_bounded_for_many_chapters():
    notes = [
        ChapterNote(
            n,
            f"Chapter {n}",
            summary="很长的摘要" * 30,
            book_so_far="全书" * 200,
            style_notes=["文体说明" * 20],
            concepts=[ConceptNote(f"term{n}", "定义" * 50)],
        )
        for n in range(1, 200)
    ]
    limits = ReadingLimits(profile_max_chars=1200)
    profile = build_profile(notes, limits)
    covered = []
    for line in profile.section_overview:
        first, _, last = re.match(r"第 (\d+)(–(\d+))? 章", line).groups()
        covered += list(range(int(first), int(last or first) + 1))
    assert covered == list(range(1, 200))  # 每章都有出处，章数太多时相邻章合成一行
    assert len(profile.to_prompt_text()) <= limits.profile_max_chars  # 画像进每块提示，整段有上界
    assert profile.terminology_hints and all("首见第" in hint for hint in profile.terminology_hints)


def test_parse_note_tolerates_noise_and_rejects_garbage():
    chapter = Chapter(2, "Dust", "text", (0, 0))
    raw = '好的：```json\n{"summary": "s", "concepts": [{"term": "hole complex", "definition": "d"}, {"bad": 1}]}\n```'
    note = parse_note(raw, chapter)
    assert note.valid and [c.term for c in note.concepts] == ["hole complex"]
    assert parse_note("not json at all", chapter).valid is False


def test_concept_terms_keep_only_words_found_in_the_source():
    index = _index("The hole complex opens. Another hole complex. Dust everywhere.\n")
    notes = [
        ChapterNote(
            1, "A", concepts=[ConceptNote("hole complex"), ConceptNote("孔洞复合体"), ConceptNote("Ghost Term")]
        )
    ]
    assert concept_terms(notes, index) == [("hole complex", 2)]


def test_reading_source_adds_terms_beyond_the_main_cap_and_is_absent_when_off():
    text = _book()
    index = _index(text)
    config = SourceConfig(sources=("proper",), max_terms=1, contexts=1, max_reading=5)
    off = gather_candidates(index, {}, text=text, config=config, zipf=lambda _w: 3.0)
    on = gather_candidates(
        index, {}, text=text, config=config, zipf=lambda _w: 3.0, reading=[("xenochronic drift", 15)]
    )
    assert [c.term for c in off] == [c.term for c in on][: len(off)]
    added = [c for c in on if c.term == "xenochronic drift"]
    assert added and added[0].sources == (READING,)


# --------------------------------------------------------------------------- #
# 流程：假模型
# --------------------------------------------------------------------------- #


class _Config:
    def __init__(self, **overrides):
        self.values = {"api.translator.max_retries": 0, "multi_agent.reading_pass.min_chapter_chars": 100, **overrides}

    def get(self, key, default=None):
        return self.values.get(key, default)


def _reader(calls: list, fail_on: set[int] | None = None):
    def run(prompt_value):
        text = prompt_value.to_string()
        number = int(re.search(r"【当前章节】: 第 (\d+) 章", text).group(1))
        calls.append({"chapter": number, "state": text.split("【当前章节】")[0]})
        if fail_on and number in fail_on:
            raise RuntimeError("boom")
        return json.dumps(
            {
                "summary": f"第{number}章摘要",
                "book_so_far": f"读到第{number}章",
                "concepts": [{"term": "xenochronic drift", "definition": "异时漂移", "where": "开头"}],
                "special_words": [{"word": "hole", "meaning": "孔洞（概念）"}],
            },
            ensure_ascii=False,
        )

    return RunnableLambda(run)


class _Store:
    def __init__(self):
        self.data = {}

    async def load(self, key):
        return self.data.get(key)

    async def save(self, key, value):
        self.data[key] = value


def test_reading_pass_reads_each_chapter_once_and_resumes_from_cache():
    text = _book(chapters=6)
    store = _Store()
    calls: list = []
    result = asyncio.run(ReadingPass(_reader(calls), _Config(), store.load, store.save).run(text))
    assert [c["chapter"] for c in calls] == list(range(1, 7))
    assert "（尚未读过任何章节）" in calls[0]["state"] and "读到第5章" in calls[5]["state"]
    assert result.usage.calls == 6 and result.usage.estimated_prompt_tokens > 0
    assert ("xenochronic drift", 72) in result.concepts
    assert result.profile.section_overview[0].startswith("第 1 章《Chapter 1: The Plateau 1》")

    again: list = []
    rerun = asyncio.run(ReadingPass(_reader(again), _Config(), store.load, store.save).run(text))
    assert again == [] and rerun.usage.cached_chapters == 6 and rerun.profile == result.profile

    changed = text.replace("paragraph 1. ", "paragraph one. ", 1).replace(
        "In chapter 5 the xenochronic drift returns as paragraph 0", "In chapter 5 something new happens"
    )
    partial: list = []
    asyncio.run(ReadingPass(_reader(partial), _Config(), store.load, store.save).run(changed))
    assert [c["chapter"] for c in partial] == [1, 2, 3, 4, 5, 6]  # 第 1 章变了，之后都要重读
    later: list = []
    tail = changed.replace("In chapter 5 something new happens", "In chapter 5 something else happens")
    asyncio.run(ReadingPass(_reader(later), _Config(), store.load, store.save).run(tail))
    assert [c["chapter"] for c in later] == [5, 6]  # 只重读改动的章及其后


def test_failed_chapter_is_skipped_and_not_cached():
    text = _book(chapters=4)
    store = _Store()
    calls: list = []
    result = asyncio.run(ReadingPass(_reader(calls, fail_on={2}), _Config(), store.load, store.save).run(text))
    assert result.usage.failed_chapters == 1 and len(result.notes) == 3
    retry: list = []
    asyncio.run(ReadingPass(_reader(retry), _Config(), store.load, store.save).run(text))
    assert [c["chapter"] for c in retry] == [2]  # 失败的章没写缓存，重跑时只补读它；其余章的原文没变，笔记照用


# --------------------------------------------------------------------------- #
# 引擎：开关
# --------------------------------------------------------------------------- #


class _Factory:
    def __init__(self, log: dict):
        self.log = log

    def create_translator(self):
        return RunnableLambda(lambda prompt: "译文")

    def create_checker(self):
        return RunnableLambda(lambda prompt: "")

    def create_analyst(self):
        def analyst(prompt_value):
            text = prompt_value.to_string() if hasattr(prompt_value, "to_string") else str(prompt_value)
            if "研究型读者" in text:
                self.log.setdefault("reading", 0)
                self.log["reading"] += 1
                return json.dumps(
                    {
                        "summary": "本章摘要",
                        "book_so_far": "全书",
                        "concepts": [{"term": "xenochronic drift", "definition": "异时漂移"}],
                    },
                    ensure_ascii=False,
                )
            if "候选" in text and "terms" in text:
                self.log["ledger_prompt"] = text
                return '{"terms": []}'
            self.log["analyst"] = self.log.get("analyst", 0) + 1
            return '{"summary": "采样画像"}'

        return RunnableLambda(analyst)


def _engine_run(tmp_path, reading: bool):
    from src.core.translator import TranslationEngine
    from src.infrastructure.cache.translation_cache import TranslationCache
    from src.utils.config_loader import Config

    config = Config(Path("config/config.yaml"))
    config.config["document_ir"]["enabled"] = False
    config.config["multi_agent"]["reading_pass"]["enabled"] = reading
    config.config["multi_agent"]["reading_pass"]["min_chapter_chars"] = 100
    log: dict = {}
    engine = TranslationEngine(
        model_factory=_Factory(log), cache=TranslationCache(tmp_path / "cache.db"), config=config
    )
    text = _book(chapters=4, paragraphs=1)

    async def run():
        await engine.cache.initialize()
        chunks = engine.plan_chunks(text)
        await engine._prepare_document(text, chunks, None)

    asyncio.run(run())
    return engine, log


def test_switched_off_reading_pass_leaves_profile_and_ledger_unchanged(tmp_path):
    engine, log = _engine_run(tmp_path, reading=False)
    assert engine.reading_result is None and "reading" not in log
    assert engine.document_profile.summary == "采样画像"
    assert "xenochronic" not in log.get("ledger_prompt", "")


def test_reading_pass_replaces_profile_and_feeds_the_ledger(tmp_path):
    engine, log = _engine_run(tmp_path, reading=True)
    assert log["reading"] == engine.reading_result.usage.chapters >= 3
    assert "analyst" not in log  # 全书画像替换了首中尾采样
    assert engine.document_profile.summary == "全书"
    assert engine.document_profile.section_overview[0].startswith("第 1 章")
    assert "xenochronic drift" in log["ledger_prompt"] and "阅读遍笔记" in log["ledger_prompt"]
    assert engine.reading_usage["calls"] == log["reading"]


# --------------------------------------------------------------------------- #
# 消融基准
# --------------------------------------------------------------------------- #


def test_bench_reports_cost_without_a_model_and_recall_with_one():
    from src.evaluation import reading_bench

    text = _book(chapters=4)
    glossary = {"xenochronic drift": "异时漂移", "desert": "沙漠"}
    cost_only = reading_bench.run(text, glossary)
    assert cost_only["usage"]["calls"] == cost_only["chapters"] and "ablation" not in cost_only
    upper = reading_bench.run(text, glossary, full_notes=True)
    assert upper["usage"]["estimated_prompt_tokens"] > cost_only["usage"]["estimated_prompt_tokens"]

    with_model = reading_bench.run(text, glossary, llm=_reader([]), folds=2)
    groups = with_model["ablation"]["groups"]
    assert groups["legacy+reading"]["found_reachable"] > groups["legacy"]["found_reachable"]
    assert "xenochronic drift" in with_model["ablation"]["added_by_reading_over_legacy"]
