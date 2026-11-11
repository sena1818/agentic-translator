"""全书索引：查询逻辑（纯函数，不依赖模型与 pandoc）+ 从结构化规划建索引、持久化与任务流程中的增量更新。"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableLambda

from src.domain.index import BookIndex, IndexedSegment, term_pattern
from src.domain.index.book_index import KEPT_ORIGINAL, STATUS_SOURCE_ONLY, STATUS_TRANSLATED, UNRESOLVED
from src.infrastructure.document import pandoc

FIXTURE = Path(__file__).parent / "fixtures" / "structured_sample.md"
needs_pandoc = pytest.mark.skipif(not pandoc.pandoc_available(), reason="需要 pandoc")


def _seg(position, source, translation=None, chunk=0, status=None):
    return IndexedSegment(
        position=position,
        chunk_index=chunk,
        source=source,
        translation=translation,
        seg_id=position,
        status=status or (STATUS_TRANSLATED if translation is not None else STATUS_SOURCE_ONLY),
    )


# --------------------------------------------------------------------------- #
# 词形匹配与出现处
# --------------------------------------------------------------------------- #


def test_term_pattern_matches_case_and_word_forms_but_not_longer_words():
    pattern = term_pattern("war machine")
    text = "War machines, a war-machine, the war machine's edge, warmachine, war machinery"
    assert [m.group(0) for m in pattern.finditer(text)] == ["War machines", "war-machine", "war machine's"]
    assert term_pattern("Mission").findall("missions mission's Missionary") == ["missions", "mission's"]
    assert term_pattern("city").findall("cities city's") == ["cities", "city's"]
    assert term_pattern("Sun", case_sensitive=True, variants=False).findall("Sun sun Suns") == ["Sun"]


def test_occurrences_page_by_segment_and_keep_total_counts():
    index = BookIndex(
        [
            _seg(0, "The Numogram has zones.", "数图有区域。"),
            _seg(1, "Nothing here."),
            _seg(2, "numogram, numograms and the Numogram again.", "数图、数图，又是数图。"),
            _seg(3, "A last numogram.", None),
        ]
    )
    first = index.occurrences("Numogram", limit=2)
    assert (first.total_matches, first.total_segments) == (5, 3)
    assert [item.segment.position for item in first.items] == [0, 2]
    assert first.items[1].before == "Nothing here." and first.items[1].after == "A last numogram."
    assert first.items[0].segment.translation == "数图有区域。"
    assert first.forms == {"numogram": 2, "Numogram": 2, "numograms": 1}
    second = index.occurrences("Numogram", offset=2, limit=2)
    assert [item.segment.position for item in second.items] == [3]
    assert second.total_matches == 5


def test_keyword_search_in_source_translation_or_both():
    index = BookIndex(
        [
            _seg(0, "Oil is a narrative agent.", "石油是叙事行动者。"),
            _seg(1, "The desert is crowded.", "沙漠很拥挤。"),
            _seg(2, "Oil and desert.", None),
        ]
    )
    assert [hit.segment.position for hit in index.search("oil desert").items] == [2]
    assert [hit.segment.position for hit in index.search("石油").items] == [0]
    assert index.search("石油", field="source").total == 0
    assert index.search("OIL", field="source").total == 2


# --------------------------------------------------------------------------- #
# 译法分布
# --------------------------------------------------------------------------- #


def _book(pairs):
    filler = [
        _seg(100 + i, f"Unrelated sentence {i}.", text)
        for i, text in enumerate(["这是无关的句子。", "虚构的故事很多。", "一个普通的段落。", "另一种虚构。"])
    ]
    return BookIndex([_seg(i, src, tgt) for i, (src, tgt) in enumerate(pairs)] + filler)


def test_renderings_count_each_translation_and_cite_segments():
    index = _book(
        [
            ("Hyperstition is a fiction.", "超虚构（Hyperstition）是一种虚构。"),
            ("The Hyperstition team met.", "超虚构团队见面了。"),
            ("Hyperstition's lab.", "超虚构的实验室。"),
            ("Hyperstition again.", "超迷信再次出现。"),
            ("hyperstition of oil", "Hyperstition of oil"),
            ("Hyperstition untranslated yet.", None),
        ]
    )
    result = index.renderings("Hyperstition", known=["超迷信"])
    counts = {item.text: item.count for item in result.renderings}
    assert counts == {"超虚构": 3, "超迷信": 1, KEPT_ORIGINAL: 1}  # "虚构"更常见于其他段，被更专用的"超虚构"取代
    assert result.segments == 5 and result.untranslated_segments == 1
    assert next(item for item in result.renderings if item.text == "超迷信").positions == (3,)


def test_renderings_mark_unresolved_instead_of_guessing():
    index = _book(
        [
            ("Their mission failed.", "他们的使命失败了。"),
            ("A mission to the east.", "一项向东的使命。"),
            ("The mission of the cult.", "该教派的使命。"),
            ("Each mission ends.", "每一个使命都会结束。"),
            ("Covert missions underway.", "秘密行动正在进行。"),  # 意译，译文里没有可对应的固定片段
        ]
    )
    result = index.renderings("mission")
    assert {item.text: item.count for item in result.renderings} == {"使命": 4, UNRESOLVED: 1}
    assert next(item for item in result.renderings if item.text == UNRESOLVED).positions == (4,)


# --------------------------------------------------------------------------- #
# 从结构化规划建索引（需要 pandoc）
# --------------------------------------------------------------------------- #


def _plan(text):
    from src.pipelines.structure.planner import StructuredDocumentPlanner

    return StructuredDocumentPlanner(chunk_size=400).plan(text).structured_chunks


@needs_pandoc
def test_source_only_index_counts_match_full_text_search():
    from src.pipelines.structure import indexing

    text = FIXTURE.read_text(encoding="utf-8")
    index = BookIndex(indexing.source_segments(_plan(text)))
    assert all(seg.status == STATUS_SOURCE_ONLY for seg in index.segments)
    # 全文搜索的对照：pandoc 纯文本输出（索引不含代码与公式，对照时去掉代码块）
    prose = re.sub(r"```[\s\S]*?```", "", text)
    plain = subprocess.run(
        ["pandoc", "-f", "markdown", "-t", "plain", "--wrap=none"], input=prose, capture_output=True, text=True
    ).stdout
    for term in ["numogram", "hyperstition", "zone", "the", "currents"]:
        assert index.occurrences(term).total_matches == len(term_pattern(term).findall(plain)), term


@needs_pandoc
def test_translated_chunks_fill_segment_translations_and_store_round_trips(tmp_path):
    from src.infrastructure.persistence.book_index_store import BookIndexStore
    from src.pipelines.structure import indexing

    chunks = _plan("Oil moves through politics.\n\nThe desert remembers oil.\n\nThe desert is quiet.\n")
    store = BookIndexStore(tmp_path / "book.sqlite")
    store.replace(indexing.source_segments(chunks), "fp")
    assert store.fingerprint() == "fp"
    store.update(indexing.translated_segments(chunks, {0: "石油穿行于政治。\n\n沙漠记得石油。\n\n沙漠很安静。\n"}))
    index = BookIndex(store.load())
    assert [seg.translation for seg in index.segments] == ["石油穿行于政治。", "沙漠记得石油。", "沙漠很安静。"]
    assert {item.text: item.count for item in index.renderings("oil").renderings} == {"石油": 2}

    store.update(indexing.translated_segments(chunks, {0: "合成一段，结构对不上。\n"}))
    assert {seg.status for seg in BookIndex(store.load()).segments} == {"unaligned"}


# --------------------------------------------------------------------------- #
# 任务流程：翻译前可查、随翻译更新、人工编辑后更新
# --------------------------------------------------------------------------- #

_SEG_RE = re.compile(r'<seg id="(\d+)">(.*?)</seg>', re.S)


def _translate(prompt_value):
    body = prompt_value.to_string().split("【待翻译段落】:")[-1]
    return "\n".join(
        f'<seg id="{i}">{text.replace("numogram", "数图").replace("Numogram", "数图")}</seg>'
        for i, text in _SEG_RE.findall(body)
    )


class _Factory:
    def create_translator(self):
        return RunnableLambda(_translate)

    def create_checker(self):
        return RunnableLambda(lambda prompt: "")

    def create_analyst(self):
        return RunnableLambda(lambda prompt: '{"summary": "测试"}')


@needs_pandoc
def test_task_index_is_queryable_before_translation_and_follows_edits(tmp_path, monkeypatch):
    from src.application.use_cases import translation_tasks
    from src.core.translator import TranslationEngine
    from src.infrastructure.cache.translation_cache import TranslationCache
    from src.infrastructure.filesystem.task_files import TaskFiles
    from src.infrastructure.persistence.database import Database
    from src.infrastructure.persistence.task_repository import TaskRepository
    from src.utils.config_loader import get_config

    config = get_config(Path("config/config.yaml"))

    def engine_factory(glossary=None, config=None):
        return TranslationEngine(
            glossary=glossary,
            config=config,
            model_factory=_Factory(),
            cache=TranslationCache(tmp_path / "cache.db"),
            engine="native",
        )

    monkeypatch.setattr(translation_tasks, "TranslationEngine", engine_factory)

    async def run():
        db = Database(str(tmp_path / "tasks.db"))
        await db.initialize()
        service = translation_tasks.TranslationService(
            repository=TaskRepository(db), files=TaskFiles(tmp_path / "data"), config=config
        )
        task = await service.create_task(FIXTURE.read_bytes(), "sample.md")

        # 翻译前：只有原文也能查
        chunks = _plan(FIXTURE.read_text(encoding="utf-8"))
        service.book_index.build_source(task.task_id, chunks)
        before = await service.term_occurrences(task.task_id, "numogram")
        assert before["total_matches"] > 0 and before["stats"] == {
            "segments": before["stats"]["segments"],
            "source_only": before["stats"]["segments"],
        }

        await service.start_translation(task.task_id)
        renderings = await service.term_renderings(task.task_id, "numogram")
        assert renderings["renderings"][0]["text"] == "数图"
        assert renderings["untranslated_segments"] == 0

        saved = await service.get_task(task.task_id)
        chunk = (await service.get_chunks(task.task_id))["chunks"][0]
        edited = chunk["translation"].replace("The decay rate", "人工审校：衰变率")
        await service.edit_chunk(task.task_id, 0, edited, saved.artifact_dir)
        hits = await service.search_book(task.task_id, "人工审校", field="translation")
        assert hits["total"] == 1 and "decay rate" in hits["items"][0]["source"]

        assert await service.delete_task(task.task_id)
        assert not service.book_index.store(task.task_id).exists()

    asyncio.run(run())
