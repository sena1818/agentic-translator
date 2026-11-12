"""纠正记忆：译名纠正识别、受保护替换、传播计划、跨书建议（纯逻辑）+ 写回块译文 + 任务流程端到端。"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableLambda

from src.domain.index.book_index import STATUS_TRANSLATED, STATUS_UNALIGNED, IndexedSegment
from src.domain.memory import (
    SCOPE_AUTHOR,
    SCOPE_BOOK,
    SCOPE_DOMAIN,
    SOURCE_GLOSSARY,
    SOURCE_HUMAN,
    SOURCE_LEDGER,
    TermEntry,
    detect_term_changes,
    plan_propagation,
    rank_suggestions,
    replace_unprotected,
)
from src.domain.memory.corrections import (
    REASON_ALREADY_NEW,
    REASON_NOT_ALIGNED,
    REASON_OTHER_RENDERING,
    REASON_PROTECTED_ONLY,
    protected_strings,
)
from src.infrastructure.document import pandoc
from src.infrastructure.persistence.memory_store import MemoryStore

needs_pandoc = pytest.mark.skipif(not pandoc.pandoc_available(), reason="需要 pandoc")

GLOSSARY = {"Numogram": "数图 (Numogram)", "Hyperstition": "超虚构 (Hyperstition)", "fiction": "虚构"}


# --------------------------------------------------------------------------- #
# 识别译名纠正
# --------------------------------------------------------------------------- #


def test_edit_that_renames_a_term_is_recognised():
    changes = detect_term_changes(
        "The Numogram maps zones. The Numogram again.",
        "数图映射区域。又是数图。",
        "数字图映射区域。又是数图。",
        GLOSSARY,
    )
    assert [(c.term, c.old_rendering, c.new_rendering, c.count) for c in changes] == [("Numogram", "数图", "数字图", 1)]


def test_edit_inside_a_longer_rendering_is_not_mistaken_for_it():
    # 改的是独立的"虚构"，"超虚构"里的那两个字没动，也不该被认作改了 Hyperstition
    changes = detect_term_changes("Hyperstition is a fiction.", "超虚构是一种虚构。", "超虚构是一种小说。", GLOSSARY)
    assert [(c.term, c.new_rendering) for c in changes] == [("fiction", "小说")]


def test_edit_without_term_in_source_or_without_rename_reports_nothing():
    assert detect_term_changes("Oil moves.", "数图在动。", "数字图在动。", GLOSSARY) == []  # 原文没有该术语
    assert detect_term_changes("The Numogram maps.", "数图映射。", "数图映射了。", GLOSSARY) == []  # 没改译名


# --------------------------------------------------------------------------- #
# 受保护替换
# --------------------------------------------------------------------------- #


def test_replacement_skips_old_rendering_inside_longer_terms():
    protected = protected_strings("虚构", "小说", ["超虚构", "虚构", "数图"])
    assert protected == ["超虚构"]
    assert replace_unprotected("超虚构是一种虚构，虚构。", "虚构", "小说", protected) == ("超虚构是一种小说，小说。", 2)


def test_replacement_is_idempotent_when_new_rendering_contains_old():
    protected = protected_strings("虚构", "超虚构", [])
    once, count = replace_unprotected("超虚构和虚构", "虚构", "超虚构", protected)
    assert (once, count) == ("超虚构和超虚构", 1)
    assert replace_unprotected(once, "虚构", "超虚构", protected) == (once, 0)


# --------------------------------------------------------------------------- #
# 传播计划
# --------------------------------------------------------------------------- #


def _seg(position, source, translation, status=STATUS_TRANSLATED, chunk=0):
    return IndexedSegment(position, chunk, source, translation, seg_id=position + 1, status=status)


def test_plan_lists_only_segments_whose_source_has_the_term():
    segments = [
        _seg(0, "The Numogram", "数图"),
        _seg(1, "Numograms map zones.", "数图映射区域。"),
        _seg(2, "Oil moves.", "石油绕过数图。"),  # 原文没有该术语：其中的"数图"不动
        _seg(3, "The Numogram again.", "又是数字图。"),
        _seg(4, "A Numogram here.", "这里是一个图表。"),
        _seg(5, "Numogram lost.", None, status=STATUS_UNALIGNED, chunk=1),
    ]
    plan = plan_propagation(segments, "Numogram", "数图", "数字图", ["数图", "超虚构"])
    assert [(item.position, item.preview, item.count) for item in plan.items] == [
        (0, "数字图", 1),
        (1, "数字图映射区域。", 1),
    ]
    assert {s.position: s.reason for s in plan.skipped} == {
        3: REASON_ALREADY_NEW,
        4: REASON_OTHER_RENDERING,
        5: REASON_NOT_ALIGNED,
    }
    assert plan.total_replacements == 2


def test_plan_reports_segments_where_old_only_appears_inside_a_longer_rendering():
    plan = plan_propagation([_seg(0, "Hyperstition, not fiction.", "超虚构")], "fiction", "虚构", "小说", ["超虚构"])
    assert plan.items == [] and plan.skipped[0].reason == REASON_PROTECTED_ONLY


def test_plan_rejects_empty_or_identical_renderings():
    with pytest.raises(ValueError):
        plan_propagation([], "Numogram", "数图", "数图")


# --------------------------------------------------------------------------- #
# 跨书建议
# --------------------------------------------------------------------------- #


def _entry(task, term, rendering, scope, source=SOURCE_GLOSSARY, author="Nick Land", domain=None, at="2026-01-01"):
    return TermEntry(task, f"book-{task}", term, rendering, scope, source, author, domain, updated_at=at)


def test_suggestions_follow_scope_and_prefer_human_decisions():
    entries = [
        _entry("t1", "Numogram", "数图 (Numogram)", SCOPE_AUTHOR, SOURCE_LEDGER, at="2026-03-01"),
        _entry("t2", "Numogram", "数字图", SCOPE_AUTHOR, SOURCE_HUMAN, at="2026-01-01"),
        _entry("t1", "Oil", "石油", SCOPE_DOMAIN, author=None, domain="philosophy"),
        _entry("t1", "Private", "私有", SCOPE_BOOK),  # 本书条目不外溢
        _entry("t3", "Lemuria", "利莫里亚", SCOPE_AUTHOR, author="Someone Else"),
        _entry("t9", "Zone", "区域", SCOPE_AUTHOR),  # 自己这本书的条目不作为建议
        _entry("t1", "Hyperstition", "超虚构", SCOPE_AUTHOR),  # 已在本书术语表里
    ]
    suggestions = rank_suggestions(
        entries, " nick land ", "Philosophy", exclude_task="t9", known_terms=["hyperstition"]
    )
    assert [(s.term, s.rendering, s.source) for s in suggestions] == [
        ("Numogram", "数字图", SOURCE_HUMAN),
        ("Oil", "石油", SOURCE_GLOSSARY),
    ]
    assert suggestions[0].label == "同作者《book-t2》·人工纠正"
    assert suggestions[1].label.startswith("同领域《book-t1》")


def test_no_author_or_domain_means_no_suggestions():
    assert rank_suggestions([_entry("t1", "Numogram", "数图", SCOPE_AUTHOR)], None, None) == []


def test_store_keeps_human_entries_over_later_automatic_ones(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    store.upsert_entries([_entry("t1", "Numogram", "数字图", SCOPE_AUTHOR, SOURCE_HUMAN)])
    store.upsert_entries([_entry("t1", "numogram", "数图", SCOPE_AUTHOR, SOURCE_LEDGER)])
    [entry] = store.entries(task_id="t1")
    assert (entry.rendering, entry.source) == ("数字图", SOURCE_HUMAN)
    assert [e.term for e in store.entries(author="NICK LAND")] == ["Numogram"]
    store.delete_task("t1")
    assert store.entries(task_id="t1") == []


# --------------------------------------------------------------------------- #
# 写回块译文
# --------------------------------------------------------------------------- #

DOC = """# The Numogram

The Numogram maps the decimal numbers onto zones.

Hyperstition is a *fiction*. The Numogram is not a fiction.

Oil moves through politics.
"""

TRANSLATIONS = {
    "The Numogram": "数图",
    "The Numogram maps the decimal numbers onto zones.": "数图把十进制数映射到区域上。",
    "Hyperstition is a <i1>fiction</i1>. The Numogram is not a fiction.": "超虚构是一种<i1>虚构</i1>。数图不是虚构。",
    "Oil moves through politics.": "石油穿行于政治，而非数图。",
}
_SEG_RE = re.compile(r'<seg id="(\d+)">(.*?)</seg>', re.S)


def _plan(text):
    from src.pipelines.structure.planner import StructuredDocumentPlanner
    from src.utils.config_loader import get_config

    return StructuredDocumentPlanner.from_config(get_config(Path("config/config.yaml"))).plan(text).structured_chunks


def _fake_translate(prompts):
    def translate(prompt_value):
        text = prompt_value.to_string()
        prompts.append(text)
        body = text.split("【待翻译段落】:")[-1]
        return "\n".join(f'<seg id="{i}">{TRANSLATIONS.get(src, src)}</seg>' for i, src in _SEG_RE.findall(body))

    return translate


@needs_pandoc
def test_text_route_changes_nothing_but_the_selected_renderings():
    from src.pipelines.structure.propagation import apply_to_chunk

    [chunk] = _plan(DOC)
    translation = "# 数图\n\n数图把十进制数映射到区域上。\n\n超虚构是一种*虚构*。数图不是虚构。\n\n石油穿行于政治。"
    edit = apply_to_chunk(chunk, translation, 0, [0, 1, 2], "数图", "数字图", [])
    assert edit.method == "text" and edit.replacements == 3
    assert edit.translation == translation.replace("数图", "数字图")


@needs_pandoc
def test_ast_route_leaves_unselected_segments_alone():
    from src.pipelines.structure.propagation import apply_to_chunk

    [chunk] = _plan(DOC)
    translation = (
        "# 数图\n\n数图把十进制数映射到区域上。\n\n超虚构是一种*虚构*。数图不是虚构。\n\n石油穿行于政治，而非数图。"
    )
    edit = apply_to_chunk(chunk, translation, 0, [1], "数图", "数字图", [])
    assert edit.method == "ast" and edit.replacements == 1
    paragraphs = [p.strip() for p in edit.translation.split("\n\n")]
    assert paragraphs == [
        "# 数图",
        "数字图把十进制数映射到区域上。",
        "超虚构是一种*虚构*。数图不是虚构。",
        "石油穿行于政治，而非数图。",
    ]


# --------------------------------------------------------------------------- #
# 任务流程：编辑 → 识别 → 预览 → 批量应用 → 同作者第二本书看到建议
# --------------------------------------------------------------------------- #


class _Factory:
    def __init__(self, prompts):
        self.prompts = prompts

    def create_translator(self):
        return RunnableLambda(_fake_translate(self.prompts))

    def create_checker(self):
        return RunnableLambda(lambda prompt: "")

    def create_analyst(self):
        return RunnableLambda(lambda prompt: '{"summary": "测试"}')


@needs_pandoc
def test_correction_propagates_within_the_book_and_reaches_the_authors_next_book(tmp_path, monkeypatch):
    from src.application.use_cases import translation_tasks
    from src.core.translator import TranslationEngine
    from src.infrastructure.cache.translation_cache import TranslationCache
    from src.infrastructure.filesystem.task_files import TaskFiles
    from src.infrastructure.persistence.database import Database
    from src.infrastructure.persistence.glossary_repository import GlossaryService
    from src.infrastructure.persistence.task_repository import TaskRepository
    from src.utils.config_loader import get_config

    config = get_config(Path("config/config.yaml"))
    prompts: list[str] = []

    def engine_factory(glossary=None, config=None):
        return TranslationEngine(
            glossary=glossary,
            config=config,
            model_factory=_Factory(prompts),
            cache=TranslationCache(tmp_path / "cache.db"),
            engine="native",
        )

    monkeypatch.setattr(translation_tasks, "TranslationEngine", engine_factory)
    glossaries = GlossaryService()
    glossaries.glossary_dir = tmp_path / "glossaries"
    glossaries.glossary_dir.mkdir()
    (glossaries.glossary_dir / "ccru.json").write_text(
        json.dumps({"Numogram": "数图 (Numogram)", "Hyperstition": "超虚构 (Hyperstition)"}), encoding="utf-8"
    )

    async def run():
        db = Database(str(tmp_path / "tasks.db"))
        await db.initialize()
        service = translation_tasks.TranslationService(
            repository=TaskRepository(db),
            files=TaskFiles(tmp_path / "data"),
            config=config,
            glossary_service=glossaries,
        )
        first = await service.create_task(DOC.encode(), "first.md", "ccru", author="Nick Land", domain="philosophy")
        assert (first.author, first.domain) == ("Nick Land", "philosophy")
        await service.start_translation(first.task_id)
        page = await service.get_chunks(first.task_id)
        [record] = page["chunks"]
        assert record["quality_report"]["passed"] is True
        original = record["translation"]

        # 1. 人工改一处译名：记为纠正，识别为 Numogram 的译名纠正；状态是"编辑过"，不是"已审校"
        edited = original.replace("数图把", "数字图把")
        result = await service.edit_chunk(first.task_id, 0, edited, page["revision"])
        assert result["term_changes"] == [
            {"term": "Numogram", "old_rendering": "数图", "new_rendering": "数字图", "count": 1}
        ]
        report = result["chunks"][0]["quality_report"]
        assert report["edited"] is True and not report.get("reviewed")
        corrections = (await service.task_corrections(first.task_id))["items"]
        assert corrections[0]["kind"] == "edit" and corrections[0]["new_text"] == "字"

        # 2. 预览：其余原文含 Numogram、译文仍是"数图"的段；"石油……而非数图"原文没有该术语，不在其中
        preview = await service.preview_term_correction(first.task_id, "Numogram", "数图", "数字图")
        assert [item["preview"] for item in preview["items"]] == ["数字图", "超虚构是一种虚构。数字图不是虚构。"]
        assert [s["reason"] for s in preview["skipped"]] == ["already_new"]

        # 3. 批量应用：只改这两处，其余文字不动；改后的块按新译名质检通过
        applied = await service.apply_term_correction(
            first.task_id, "Numogram", "数图", "数字图", None, preview["revision"]
        )
        assert applied["replacements"] == 2 and applied["scope"] == "author"
        assert all(chunk["passed"] for chunk in applied["chunks"])
        [after] = (await service.get_chunks(first.task_id))["chunks"]
        # 只有这两处变了：与"把编辑后译文里所有数图换成数字图、再把石油那句还原"逐字相同
        assert after["translation"] == edited.replace("数图", "数字图").replace("而非数字图", "而非数图")
        assert "石油穿行于政治，而非数图。" in after["translation"]
        assert after["quality_report"]["passed"] is True and after["quality_report"]["propagated"] == "Numogram"
        renderings = await service.term_renderings(first.task_id, "numogram")
        assert [r["text"] for r in renderings["renderings"]] == ["数字图"]
        ledger = json.loads((glossaries.glossary_dir / "first_术语账本.json").read_text(encoding="utf-8"))
        assert ledger["Numogram"] == "数字图"

        # 4. 审校是另一种状态
        page = await service.get_chunks(first.task_id)
        reviewed = await service.mark_reviewed(first.task_id, 0, page["revision"])
        assert reviewed["chunks"][0]["quality_report"]["reviewed"] is True

        # 5. 同作者的第二本书：能看到来自第一本书的建议并标明来源；建议进提示但不参与术语校验
        second = await service.create_task(DOC.encode(), "second.md", author="nick land")
        suggestions = (await service.term_suggestions(second.task_id))["items"]
        by_term = {item["term"]: item for item in suggestions}
        assert by_term["Numogram"]["rendering"] == "数字图"
        assert by_term["Numogram"]["label"] == "同作者《first》·人工纠正"
        assert by_term["Hyperstition"]["label"] == "同作者《first》·术语表"

        prompts.clear()
        await service.start_translation(second.task_id)
        assert any("【参考译名】" in p and "- Numogram: 数字图（同作者《first》·人工纠正）" in p for p in prompts)
        [record] = (await service.get_chunks(second.task_id))["chunks"]
        assert "数图" in record["translation"]  # 假翻译员没采纳建议
        assert not any(issue["kind"] == "terminology" for issue in record["quality_report"]["issues"])
        task = await service.get_task(second.task_id)
        assert task.quality_summary["term_suggestions"] == len(suggestions)

        # 不同作者、未填领域的书看不到
        other = await service.create_task(DOC.encode(), "third.md", author="Someone Else")
        assert (await service.term_suggestions(other.task_id))["items"] == []

    asyncio.run(run())
