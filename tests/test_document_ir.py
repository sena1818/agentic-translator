"""
Document IR（结构化翻译）测试

覆盖：行内标签编解码、AST 规划与回填、段级质检与定点修复、结构化导出
（单语 / 段级双语 / 双栏联动 HTML）、人工审校改动结构后的降级、锚点保留。
需要 pandoc；未安装时整组跳过。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableLambda

from src.domain.document import document_ir, inline_codec
from src.infrastructure.document import pandoc

pytestmark = pytest.mark.skipif(not pandoc.pandoc_available(), reason="需要 pandoc")

FIXTURE = Path(__file__).parent / "fixtures" / "structured_sample.md"
_SEG_RE = re.compile(r'<seg id="(\d+)">(.*?)</seg>', re.S)


def _segments_from_prompt(prompt_value) -> list[tuple[str, str]]:
    body = prompt_value.to_string().split("【待翻译段落】:")[-1]
    return _SEG_RE.findall(body)


def _translate_all(prompt_value) -> str:
    """假翻译员：给每段加"译:"前缀并原样保留标签。"""
    return "\n".join(f'<seg id="{i}">译:{text}</seg>' for i, text in _segments_from_prompt(prompt_value))


class FakeFactory:
    def __init__(self, translator=_translate_all, checker=None):
        self.translator = translator
        self.checker = checker or (lambda prompt: "")

    def create_translator(self):
        return RunnableLambda(self.translator)

    def create_checker(self):
        return RunnableLambda(self.checker)

    def create_analyst(self):
        return RunnableLambda(lambda prompt: '{"summary": "测试"}')


def _config():
    from src.utils.config_loader import get_config

    config = get_config(Path("config/config.yaml"))
    assert config.get("document_ir.enabled") is True
    return config


def _engine(tmp_path, **factory_kwargs):
    from src.core.translator import TranslationEngine
    from src.infrastructure.cache.translation_cache import TranslationCache

    return TranslationEngine(
        model_factory=FakeFactory(**factory_kwargs),
        cache=TranslationCache(tmp_path / "cache.db"),
        config=_config(),
        engine="native",
    )


def _translate(engine, text, tmp_path):
    chunks = engine.plan_chunks(text)
    return chunks, asyncio.run(engine.translate_batch(text, tmp_path / "stream.md", prepared_chunks=chunks))


# --------------------------------------------------------------------------- #
# 行内标签编解码
# --------------------------------------------------------------------------- #


def _para_inlines(markdown: str) -> list[dict]:
    return pandoc.markdown_to_ast(markdown)["blocks"][0]["c"]


def test_codec_roundtrip_preserves_every_inline_structure():
    inlines = _para_inlines(
        'The *CCRU* saw **hyper** [link](http://x.com "t") $x^2$ `code` "quoted"[^1] x<y & z.\n\n[^1]: Note.\n'
    )
    encoded = inline_codec.encode(inlines)
    assert "x^2" not in encoded.text and "http://x.com" not in encoded.text
    assert "&lt;" in encoded.text and "&amp;" in encoded.text

    decoded = inline_codec.decode(encoded.text, inlines)
    assert decoded.problems == []
    assert inline_codec.skeleton(decoded.inlines) == inline_codec.skeleton(inlines)
    assert inline_codec.plain_text(decoded.inlines) == inline_codec.plain_text(inlines)


def test_decode_allows_reordering_and_reports_lost_structure():
    inlines = _para_inlines("Alpha **beta** gamma $E=mc^2$ [delta](http://d.io).")
    encoded = inline_codec.encode(inlines).text
    pair = re.search(r"<(b\d+)>beta</\1>", encoded).group(1)
    math = re.search(r"<(m\d+)/>", encoded).group(1)
    link = re.search(r"<(a\d+)>", encoded).group(1)

    reordered = f"<{link}>德尔塔</{link}> 先于 <{pair}>贝塔</{pair}>，公式 <{math}/>。"
    decoded = inline_codec.decode(reordered, inlines)
    assert decoded.problems == []
    assert inline_codec.compare_skeletons(inlines, decoded.inlines) == ([], [])

    lost = inline_codec.decode(f"<{pair}>贝塔</{pair}> 丢了公式和链接 <x99/>", inlines)
    assert any("未知标签" in problem for problem in lost.problems)
    severe, _ = inline_codec.compare_skeletons(inlines, lost.inlines)
    assert any("公式" in item for item in severe) and any("链接" in item for item in severe)


def test_decode_recovers_from_unclosed_and_stray_tags():
    inlines = _para_inlines("A *b* c.")
    tag = re.search(r"<(i\d+)>", inline_codec.encode(inlines).text).group(1)
    decoded = inline_codec.decode(f"甲 <{tag}>乙 丙 </{tag}></{tag}>", inlines)
    assert any("多余" in problem for problem in decoded.problems)
    unclosed = inline_codec.decode(f"甲 <{tag}>乙", inlines)
    assert any("未闭合" in problem for problem in unclosed.problems)
    assert inline_codec.plain_text(unclosed.inlines) == "甲 乙"


# --------------------------------------------------------------------------- #
# 规划与回填
# --------------------------------------------------------------------------- #


def test_plan_and_backfill_keeps_structure_and_translates_footnotes():
    ast = pandoc.markdown_to_ast(FIXTURE.read_text(encoding="utf-8"))
    ir = document_ir.DocumentIR(ast, document_ir.plan_units(ast, 3600))
    chunks = document_ir.plan_chunks(ir, 3200, 1200)
    blocks = [block for chunk in chunks for block in chunk.blocks]
    assert len(blocks) == len(ast["blocks"])  # 单元覆盖全文，代码块/公式块也在

    segments = [seg for chunk in chunks for seg in chunk.segments]
    assert [seg.seg_id for seg in segments] == list(range(len(segments)))
    assert any(seg.in_note for seg in segments)
    assert not any("zone(n)" in seg.tagged for seg in segments)  # 代码不送 LLM

    translated = []
    for chunk in chunks:
        replacements = {seg.candidate_index: "译:" + seg.tagged for seg in chunk.segments}
        translated += document_ir.translate_blocks(chunk.blocks, replacements)
    markdown = pandoc.blocks_to_markdown(ast, translated)

    assert "# 译:Hyperstition {#hyperstition}" in markdown
    assert "[^1]: 译:Nick Land" in markdown
    assert "$N(t) = N_0 e^{-\\lambda t}$" in markdown
    assert "\\int_0^\\infty e^{-x^2}" in markdown
    assert "![译:The numogram diagram](images/numogram.png)" in markdown
    assert "# keep this comment untouched" in markdown
    assert "(http://ccru.net/archive.htm)" in markdown
    assert document_ir.collect_link_report(translated)["dead_links"] == []


def test_oversized_div_is_split_into_child_units():
    paragraphs = "\n\n".join(f"Paragraph number {i} " + "word " * 60 for i in range(12))
    ast = pandoc.markdown_to_ast(f"::: {{#chapter .wrapper}}\n{paragraphs}\n:::\n")
    units = document_ir.plan_units(ast, 1000)
    assert len(units) == 12 and all(len(unit.path) == 2 for unit in units)
    chunks = document_ir.plan_chunks(document_ir.DocumentIR(ast, units), 1000, 400)
    assert len(chunks) > 1

    blocks = __import__("copy").deepcopy(ast["blocks"])
    for unit in reversed(units):
        document_ir.set_block_at(blocks, unit.path, [{"t": "Para", "c": [{"t": "Str", "c": "X"}]}])
    assert blocks[0]["t"] == "Div" and blocks[0]["c"][0][0] == "chapter"
    assert len(blocks[0]["c"][1]) == 12


# --------------------------------------------------------------------------- #
# 引擎：段级质检与定点修复
# --------------------------------------------------------------------------- #


def _drop_math(prompt_value) -> str:
    return re.sub(r"<m\d+/>", "", _translate_all(prompt_value))


def test_engine_translates_structured_chunks(tmp_path):
    engine = _engine(tmp_path)
    chunks, results = _translate(engine, FIXTURE.read_text(encoding="utf-8"), tmp_path)
    assert engine.document_plan is not None and all(chunk.structured for chunk in chunks)
    assert all(result.success for result in results)
    joined = "\n".join(result.translation for result in results)
    assert "译:The decay rate obeys $N(t) = N_0 e^{-\\lambda t}$" in joined
    assert results[0].quality_report["passed"] is True


def test_lost_formula_is_repaired_by_targeted_review(tmp_path):
    repaired_ids = []

    def checker(prompt_value):
        body = prompt_value.to_string().split("【待修复段落】:")[-1]
        out = []
        for local_id, source in re.findall(r"【段 (\d+)】\n原文：(.*?)\n当前译文", body, re.S):
            repaired_ids.append(local_id)
            out.append(f'<seg id="{local_id}">修复:{source}</seg>')
        return "\n".join(out)

    engine = _engine(tmp_path, translator=_drop_math, checker=checker)
    _, results = _translate(engine, FIXTURE.read_text(encoding="utf-8"), tmp_path)
    result = results[0]
    assert result.repaired is True
    assert result.quality_report["passed"] is True
    assert len(repaired_ids) == 1  # 只有丢了公式的那一段被送去修复（纯公式的表格单元不是可译段）
    assert "修复:The decay rate obeys $N(t)" in result.translation


def test_unrepairable_segment_falls_back_to_source_text(tmp_path):
    engine = _engine(tmp_path, translator=_drop_math, checker=lambda prompt: "")
    _, results = _translate(engine, FIXTURE.read_text(encoding="utf-8"), tmp_path)
    result = results[0]
    assert result.success is True and result.quality_report["passed"] is False
    assert "The decay rate obeys $N(t) = N_0 e^{-\\lambda t}$" in result.translation  # 保留原文，公式不丢
    issues = result.quality_report["issues"]
    assert any("保留原文" in issue["message"] and "segment" in issue for issue in issues)


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #


def _export(tmp_path, records, text=None):
    from src.pipelines.structure.exporter import StructuredExporter

    exporter = StructuredExporter.from_config(_config())
    stats = exporter.export_all(
        text or FIXTURE.read_text(encoding="utf-8"), records, tmp_path / "out", "doc", "测试", bilingual=True
    )
    return stats, tmp_path / "out"


def test_export_writes_aligned_markdown_and_linked_reader(tmp_path):
    engine = _engine(tmp_path)
    _, results = _translate(engine, FIXTURE.read_text(encoding="utf-8"), tmp_path)
    stats, out = _export(tmp_path, [asdict(result) for result in results])

    assert stats["aligned_chunks"] == stats["chunks"] and stats["dead_links"] == []
    mono = (out / "doc.md").read_text(encoding="utf-8")
    assert mono.startswith("# 译:Contents")
    assert "[^1]: 译:Nick Land" in mono

    bilingual = (out / "doc.bilingual.md").read_text(encoding="utf-8")
    assert "> **Hyperstition**" in bilingual and "# 译:Hyperstition {#hyperstition}" in bilingual
    assert bilingual.count("](images/numogram.png)") == 1  # 图片只出现一次，原文侧只引用图注

    reader = (out / "doc.html").read_text(encoding="utf-8")
    source_pane, target_pane = reader.split('id="pane-src"')[1].split('id="pane-tgt"')
    source_ids = re.findall(r'data-seg="(\d+)"', source_pane)
    target_ids = re.findall(r'data-seg="(\d+)"', target_pane)
    assert source_ids and sorted(source_ids, key=int) == sorted(set(target_ids), key=int)
    assert 'id="src-hyperstition"' in reader and 'id="tgt-hyperstition"' in reader
    assert "<math" in reader  # MathML，离线可用


def test_export_marks_issues_and_failed_chunks(tmp_path):
    engine = _engine(tmp_path, translator=_drop_math, checker=lambda prompt: "")
    _, results = _translate(engine, FIXTURE.read_text(encoding="utf-8"), tmp_path)
    stats, out = _export(tmp_path, [asdict(result) for result in results])
    assert stats["segments_with_issues"] >= 1
    assert "seg-issue" in (out / "doc.html").read_text(encoding="utf-8")

    failed = [{**asdict(result), "success": False, "translation": "[翻译失败]"} for result in results]
    stats, out = _export(tmp_path, failed)
    assert stats["failed_chunks"] == stats["chunks"]
    assert "seg-failed" in (out / "doc.html").read_text(encoding="utf-8")
    assert "The *CCRU* defined" in (out / "doc.md").read_text(encoding="utf-8")


def test_human_edit_that_changes_structure_degrades_to_chunk_alignment(tmp_path):
    engine = _engine(tmp_path)
    _, results = _translate(engine, FIXTURE.read_text(encoding="utf-8"), tmp_path)
    records = [asdict(result) for result in results]
    records[0]["translation"] = "人工把整块改写成了一段话。"
    stats, out = _export(tmp_path, records)
    assert stats["unaligned_chunks"] == 1
    assert "人工把整块改写成了一段话。" in (out / "doc.md").read_text(encoding="utf-8")


def test_export_rejects_records_from_a_different_plan(tmp_path):
    from src.pipelines.structure.exporter import StructuredExporter

    exporter = StructuredExporter.from_config(_config())
    assert exporter.assemble("Other text.", [{"chunk_index": 0, "chunk_id": "chunk-x", "success": True}]) is None


# --------------------------------------------------------------------------- #
# 预处理：目录锚点
# --------------------------------------------------------------------------- #


def test_cleaner_keeps_anchors_that_links_point_to():
    from src.pipelines.preprocess.artifact_cleaner import EpubArtifactCleaner

    text = "- [Chapter](#ch1)\n\n[]{#ch1}\n\n# Chapter\n\n[]{#kindle_noise}\n\nBody.\n"
    cleaned = EpubArtifactCleaner().clean(text, source_type="epub")
    assert "[]{#ch1}" in cleaned
    assert "kindle_noise" not in cleaned


# --------------------------------------------------------------------------- #
# 任务流：翻译 → 结构化导出 → 人工审校重渲染
# --------------------------------------------------------------------------- #


def test_task_workflow_publishes_reader_and_rerenders_after_edit(tmp_path, monkeypatch):
    from src.application.use_cases import translation_tasks
    from src.core.translator import TranslationEngine
    from src.domain.models.task_models import TaskStatus
    from src.infrastructure.cache.translation_cache import TranslationCache
    from src.infrastructure.filesystem.task_files import TaskFiles
    from src.infrastructure.persistence.database import Database
    from src.infrastructure.persistence.task_repository import TaskRepository

    def engine_factory(glossary=None, config=None):
        return TranslationEngine(
            glossary=glossary,
            config=config,
            model_factory=FakeFactory(),
            cache=TranslationCache(tmp_path / "cache.db"),
            engine="native",
        )

    monkeypatch.setattr(translation_tasks, "TranslationEngine", engine_factory)

    async def run():
        db = Database(str(tmp_path / "tasks.db"))
        await db.initialize()
        service = translation_tasks.TranslationService(
            repository=TaskRepository(db), files=TaskFiles(tmp_path / "data"), config=_config()
        )
        task = await service.create_task(FIXTURE.read_bytes(), "sample.md", bilingual=False)
        await service.start_translation(task.task_id)
        saved = await service.get_task(task.task_id)
        assert saved.status == TaskStatus.COMPLETED
        artifacts = service.files.artifacts(saved)
        assert (artifacts / "source.md").exists()
        assert (artifacts / f"{task.task_id}.structure.json").exists()
        assert 'data-seg="0"' in (artifacts / f"{task.task_id}.html").read_text(encoding="utf-8")

        chunk = (await service.get_chunks(task.task_id))["chunks"][0]
        edited = chunk["translation"].replace("译:The decay rate", "人工审校：衰变率")
        await service.edit_chunk(task.task_id, 0, edited, saved.artifact_dir)
        republished = service.files.artifacts(await service.get_task(task.task_id))
        assert republished != artifacts and (republished / "source.md").exists()
        assert "人工审校：衰变率" in (republished / f"{task.task_id}.md").read_text(encoding="utf-8")
        assert "人工审校：衰变率" in (republished / f"{task.task_id}.html").read_text(encoding="utf-8")

    asyncio.run(run())


def test_reader_route_serves_sandboxed_html_for_mono_tasks(tmp_path, monkeypatch):
    from datetime import datetime

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.database.db import Database
    from src.api.models.task import TaskProgress, TaskStatus, TranslationTask
    from src.api.routes import files as files_routes

    run_dir = tmp_path / "data" / "results" / "runs" / "ir-task" / "exec-1"
    run_dir.mkdir(parents=True)
    (run_dir / "ir-task.md").write_text("# 译文\n", encoding="utf-8")
    (run_dir / "ir-task.json").write_text("[]", encoding="utf-8")
    (run_dir / "ir-task.structure.json").write_text("{}", encoding="utf-8")
    (run_dir / "ir-task.html").write_text("<!DOCTYPE html><title>reader</title>", encoding="utf-8")

    async def seed(db):
        await db.initialize()
        await db.save_task(
            TranslationTask(
                task_id="ir-task",
                filename="book.md",
                status=TaskStatus.COMPLETED,
                bilingual=False,
                progress=TaskProgress(current=1, total=1, percentage=100.0),
                created_at=datetime.now(),
                updated_at=datetime.now(),
                artifact_dir=str(run_dir),
            )
        )

    db = Database(str(tmp_path / "translation.db"))
    asyncio.run(seed(db))
    monkeypatch.setattr(files_routes, "db", db)
    monkeypatch.chdir(tmp_path)
    app = FastAPI()
    app.include_router(files_routes.router)
    response = TestClient(app).get("/api/files/results/ir-task?format=html&inline=true")
    assert response.status_code == 200
    assert "reader" in response.text
    assert response.headers["content-disposition"].startswith("inline")
    assert "sandbox allow-scripts" in response.headers["content-security-policy"]


# --------------------------------------------------------------------------- #
# 真实模型评测暴露的规则误报（Gemini 3.5 Flash，structured_chapter.md）
# --------------------------------------------------------------------------- #


def test_glossary_matching_is_whole_word_and_optionally_case_aware():
    from src.domain.rules.glossary_selection import term_occurs

    assert not term_occurs("Sun", "every Sunday")
    assert not term_occurs("Oil", "the toil of reading")
    assert term_occurs("Plot Hole", "Plot Holes are gaps")
    assert term_occurs("Backpropagation", "we use backpropagation")  # 默认大小写无关（论文）
    assert not term_occurs("Earth", "openings in the earth", respect_case=True)  # 理论著作：小写是普通用词
    assert term_occurs("Earth", "the Earth turns", respect_case=True)


def test_numbers_adjacent_to_cjk_are_not_reported_missing():
    from src.core.validator import TranslationValidator

    report = TranslationValidator().validate("Published in 2008, 47 pages.", "2008年出版，共47页。", {})
    assert not any(issue.kind == "fidelity" for issue in report.issues)


def test_italic_titles_rendered_with_chinese_brackets_are_not_flagged():
    inlines = _para_inlines('See *Cyclonopedia* and "the blog".')
    translated = _para_inlines("见《旋风百科全书》与“博客”。")
    assert inline_codec.compare_skeletons(inlines, translated) == ([], [])


def test_table_of_contents_follows_translated_headings():
    from src.pipelines.structure.exporter import sync_toc_entries

    ast = pandoc.markdown_to_ast(
        "- [Part One](#one)\n\n# 第一部分：开端 {#one}\n\nSee [the first part](#one) for details.\n"
    )
    assert sync_toc_entries(ast["blocks"]) == 1
    markdown = pandoc.blocks_to_markdown(ast, ast["blocks"])
    assert "-   [第一部分：开端](#one)" in markdown
    assert "See [the first part](#one)" in markdown  # 正文交叉引用保留原措辞
