"""
交付形式：离线阅读器每张图只内联一次、缺图进导出统计、EPUB（仅译文 / 双语）、下载与统计接口。
需要 pandoc；未安装时整组跳过。
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import zipfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableLambda

from src.infrastructure.document import pandoc

pytestmark = pytest.mark.skipif(not pandoc.pandoc_available(), reason="需要 pandoc")

FIXTURE = Path(__file__).parent / "fixtures" / "structured_sample.md"
_SEG_RE = re.compile(r'<seg id="(\d+)">(.*?)</seg>', re.S)
# 1×1 透明 PNG
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def _translate_all(prompt_value) -> str:
    body = prompt_value.to_string().split("【待翻译段落】:")[-1]
    return "\n".join(f'<seg id="{i}">译:{text}</seg>' for i, text in _SEG_RE.findall(body))


class _Factory:
    def create_translator(self):
        return RunnableLambda(_translate_all)

    def create_checker(self):
        return RunnableLambda(lambda prompt: "")

    def create_analyst(self):
        return RunnableLambda(lambda prompt: '{"summary": "测试"}')


@pytest.fixture(scope="module")
def records(tmp_path_factory):
    from src.core.translator import TranslationEngine
    from src.infrastructure.cache.translation_cache import TranslationCache
    from src.utils.config_loader import get_config

    tmp = tmp_path_factory.mktemp("engine")
    engine = TranslationEngine(
        model_factory=_Factory(),
        cache=TranslationCache(tmp / "cache.db"),
        config=get_config(Path("config/config.yaml")),
    )
    text = FIXTURE.read_text(encoding="utf-8")
    chunks = engine.plan_chunks(text)
    results = asyncio.run(engine.translate_batch(text, tmp / "stream.md", prepared_chunks=chunks))
    return [asdict(result) for result in results]


def _export(tmp_path, records, with_image=True):
    from src.pipelines.postprocess.export_service import ExportService
    from src.pipelines.structure.exporter import StructuredExporter
    from src.utils.config_loader import get_config

    assets = tmp_path / "assets"
    (assets / "images").mkdir(parents=True)
    if with_image:
        (assets / "images" / "numogram.png").write_bytes(_PNG)
    exporter = StructuredExporter.from_config(get_config(Path("config/config.yaml")))
    out = tmp_path / "out"
    stats = exporter.export_all(
        FIXTURE.read_text(encoding="utf-8"),
        records,
        out,
        "doc",
        "测试",
        bilingual=True,
        image_locator=ExportService.image_locator([assets]),
    )
    return stats, out


def test_reader_inlines_each_image_once_and_both_panes_share_it(tmp_path, records):
    stats, out = _export(tmp_path, records)
    reader = (out / "doc.html").read_text(encoding="utf-8")
    assert reader.count("data:image/png;base64,") == 1
    source_pane, target_pane = reader.split('id="pane-src"')[1].split('id="pane-tgt"')
    assert 'data-img="i0"' in source_pane and 'data-img="i0"' in target_pane
    assert 'id="reader-images"' in reader
    assert stats["missing_images"] == []


def test_missing_images_are_listed_in_stats_and_reader(tmp_path, records):
    stats, out = _export(tmp_path, records, with_image=False)
    assert stats["missing_images"] == ["images/numogram.png"]
    assert "1 张图片缺失" in (out / "doc.html").read_text(encoding="utf-8")
    assert '"missing_images": [\n    "images/numogram.png"' in (out / "doc.structure.json").read_text(encoding="utf-8")


def _epub_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        assert archive.infolist()[0].filename == "mimetype"
        assert archive.read("mimetype") == b"application/epub+zip"
        return archive.namelist()


def _epub_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        return "\n".join(
            archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith((".xhtml", ".ncx"))
        )


def test_epub_has_toc_images_and_footnote_links(tmp_path, records):
    stats, out = _export(tmp_path, records)
    assert stats["epub"] == ["mono", "bilingual"]

    names = _epub_names(out / "doc.epub")
    assert any(name.endswith("nav.xhtml") for name in names)
    assert any(name.startswith("EPUB/media/") and name.endswith(".png") for name in names)
    text = _epub_text(out / "doc.epub")
    assert "译:Hyperstition" in text and "译:The Numogram" in text  # 目录与正文
    assert 'epub:type="noteref"' in text and "译:Nick Land" in text  # 脚注引用与脚注内容

    bilingual = _epub_text(out / "doc.bilingual.epub")
    assert "译:Hyperstition" in bilingual


def _epub_css(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        return "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith(".css"))


def test_bilingual_epub_sets_source_apart_from_translation(tmp_path, records):
    """原文段放进 class="source" 的容器并单独设样式，不再借用引用块，与书里本来的引文区分开。"""
    _, out = _export(tmp_path, records)
    bilingual = _epub_text(out / "doc.bilingual.epub")
    # 每个原文段紧跟它的译文
    pairs = re.findall(r'<div class="source">\s*<p>(.*?)</p>\s*</div>\s*<p>(.*?)</p>', bilingual, re.S)
    assert pairs
    assert all(target.startswith("译:") and not source.startswith("译:") for source, target in pairs)
    # 书里本来的引文在原文侧、译文侧都还是引用块，原文侧外面多一层 source 容器
    assert re.search(r'<div class="source">\s*<blockquote>', bilingual)
    assert re.search(r'</div>\s*<blockquote>\s*<p>译:', bilingual)

    css = _epub_css(out / "doc.bilingual.epub")
    assert "div.source" in css
    assert "@page" in css  # 默认样式仍在，只是追加
    assert "div.source" not in _epub_css(out / "doc.epub")


def test_epub_marks_missing_images_instead_of_leaving_broken_links(tmp_path, records):
    stats, out = _export(tmp_path, records, with_image=False)
    names = _epub_names(out / "doc.epub")
    assert not any(name.startswith("EPUB/media/") for name in names)
    assert "[图片缺失：" in _epub_text(out / "doc.epub")
    assert stats["missing_images"] == ["images/numogram.png"]


@pytest.mark.skipif(shutil.which("epubcheck") is None, reason="需要 epubcheck")
def test_epub_passes_epubcheck(tmp_path, records):
    _, out = _export(tmp_path, records)
    for name in ("doc.epub", "doc.bilingual.epub"):
        completed = subprocess.run(["epubcheck", str(out / name)], capture_output=True, text=True, check=False)
        assert completed.returncode == 0, completed.stdout + completed.stderr


def _client(tmp_path, monkeypatch, run_dir: Path | None, task_id: str):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.database.db import Database
    from src.api.models.task import TaskProgress, TaskStatus, TranslationTask
    from src.api.routes import files as files_routes

    async def seed(db):
        await db.initialize()
        await db.save_task(
            TranslationTask(
                task_id=task_id,
                filename="book.md",
                status=TaskStatus.COMPLETED,
                bilingual=True,
                progress=TaskProgress(current=1, total=1, percentage=100.0),
                created_at=datetime.now(),
                updated_at=datetime.now(),
                artifact_dir=str(run_dir) if run_dir else None,
            )
        )

    db = Database(str(tmp_path / "translation.db"))
    asyncio.run(seed(db))
    monkeypatch.setattr(files_routes, "db", db)
    monkeypatch.chdir(tmp_path)
    app = FastAPI()
    app.include_router(files_routes.router)
    return TestClient(app)


def test_epub_download_and_structure_route(tmp_path, monkeypatch):
    run_dir = tmp_path / "data" / "results" / "runs" / "ir-task" / "exec-1"
    run_dir.mkdir(parents=True)
    (run_dir / "ir-task.epub").write_bytes(b"PK-mono")
    (run_dir / "ir-task.bilingual.epub").write_bytes(b"PK-bilingual")
    (run_dir / "ir-task.structure.json").write_text(
        '{"segments": 3, "missing_images": ["a.png"], "epub": ["mono", "bilingual"]}', encoding="utf-8"
    )
    client = _client(tmp_path, monkeypatch, run_dir, "ir-task")

    mono = client.get("/api/files/results/ir-task?format=epub")
    assert mono.status_code == 200 and mono.content == b"PK-mono"
    assert mono.headers["content-type"] == "application/epub+zip"
    assert "book.cn.epub" in mono.headers["content-disposition"]
    bilingual = client.get("/api/files/results/ir-task?format=epub&variant=bilingual")
    assert bilingual.content == b"PK-bilingual"

    structure = client.get("/api/files/results/ir-task/structure").json()
    assert structure["missing_images"] == ["a.png"] and structure["epub"] == ["mono", "bilingual"]


def test_legacy_task_has_no_epub_or_structure(tmp_path, monkeypatch):
    run_dir = tmp_path / "data" / "results"
    run_dir.mkdir(parents=True)
    (run_dir / "old-task.md").write_text("# 旧译文\n", encoding="utf-8")
    client = _client(tmp_path, monkeypatch, None, "old-task")
    assert client.get("/api/files/results/old-task?format=epub").status_code == 404
    assert client.get("/api/files/results/old-task/structure").status_code == 404
    assert client.get("/api/files/results/old-task?format=md").status_code == 200


def test_demo_builds_from_public_domain_sources(tmp_path):
    """演示页的原文与人工译文块结构一致，导出完整（无缺图、无未对齐块、两种 EPUB）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_demo", Path("scripts/build_demo.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--out", str(tmp_path / "site")]) == 0
    built = sorted(path.name for path in (tmp_path / "site").iterdir())
    assert built == ["index.html", "walden.bilingual.epub", "walden.epub"]
    page = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert page.count("data:image/svg+xml;base64,") == 1 and "我到林中去" in page
