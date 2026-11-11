"""
生成公开演示页：用公版文本（梭罗《瓦尔登湖》节选）和人工译文走一遍结构化导出，
产出对照阅读器 index.html 与两种 EPUB，供 GitHub Pages 发布。

不调用模型：译文是 docs/demo/translation.md，与原文块结构一一对应；脚本按结构化规划
把译文切成与翻译记录相同形状的块，再交给正式的导出器。有版权的书不要放进这里。

    python scripts/build_demo.py            # 写到 docs/demo/site/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.domain.document import document_ir
from src.infrastructure.document import pandoc
from src.pipelines.postprocess.export_service import ExportService
from src.pipelines.structure.exporter import StructuredExporter
from src.pipelines.structure.planner import StructuredDocumentPlanner
from src.utils.config_loader import get_config

DEMO = ROOT / "docs" / "demo"
STEM = "walden"


def build_records(source: str, translation: str, planner: StructuredDocumentPlanner) -> list[dict]:
    """把与原文同结构的译文切成每块一条的翻译记录。"""
    plan = planner.plan(source)
    translated_ast = pandoc.markdown_to_ast(translation)
    records = []
    for chunk in plan.chunks:
        blocks = [document_ir.get_block_at(translated_ast["blocks"], unit.path) for unit in chunk.structured.units]
        records.append(
            {
                "chunk_index": chunk.index,
                "chunk_id": chunk.chunk_id,
                "original": chunk.text,
                "translation": pandoc.blocks_to_markdown(translated_ast, blocks),
                "success": True,
                "quality_report": {"passed": True, "issues": []},
            }
        )
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成公开演示页（对照阅读器 + EPUB）")
    parser.add_argument("--out", type=Path, default=DEMO / "site")
    args = parser.parse_args(argv)

    config = get_config(ROOT / "config" / "config.yaml")
    source = (DEMO / "source.md").read_text(encoding="utf-8")
    translation = (DEMO / "translation.md").read_text(encoding="utf-8")
    exporter = StructuredExporter.from_config(config)
    records = build_records(source, translation, exporter.planner)

    out = args.out
    stats = exporter.export_all(
        source,
        records,
        out,
        STEM,
        "瓦尔登湖（节选）· 对照阅读演示",
        bilingual=True,
        image_locator=ExportService.image_locator([DEMO]),
    )
    if stats is None:
        raise SystemExit("译文与原文的块结构对不上，检查 docs/demo/translation.md")
    if stats["missing_images"] or stats["unaligned_chunks"] or stats["dead_links"]:
        raise SystemExit(f"演示导出不完整：{stats}")
    (out / f"{STEM}.html").replace(out / "index.html")
    for leftover in (f"{STEM}.md", f"{STEM}.bilingual.md", f"{STEM}.structure.json"):
        (out / leftover).unlink(missing_ok=True)
    print(f"演示页已生成：{out / 'index.html'}；EPUB：{', '.join(stats['epub'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
