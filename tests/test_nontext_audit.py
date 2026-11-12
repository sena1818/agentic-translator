"""
全书非文字审计：给定原文与译文 AST 得到哪些问题、问题来自原文还是翻译。

注入的丢图、改公式、改表格数字、错编号、断脚注必须全部检出；原样译文不报翻译问题。
需要 pandoc 把 Markdown 解析成 AST；审计本身不调用模型。
"""

from __future__ import annotations

import pytest

from src.domain.document.nontext_audit import AuditUnit, audit_document
from src.infrastructure.document import pandoc

pytestmark = pytest.mark.skipif(not pandoc.pandoc_available(), reason="需要 pandoc")

SOURCE = r"""# 2 Method {#method}

As shown in Figure 1 and Table 1, the loss follows Equation (1).[^1]

![Figure 1: The training curve](images/curve.png)

$$L = \sum_i (y_i - \hat{y}_i)^2 \tag{1}$$

| Model | Layers | Accuracy |
|-------|--------|----------|
| Small | 12 | 70.5 |
| Large | 24 | 80.1 |

: Table 1: Results on the held-out set.

```python
loss = criterion(output, target)
```

See [the method](#method) and Section 2 for details.

[^1]: The derivation follows standard practice.
"""

TARGET = r"""# 2 方法 {#method}

如图 1 和表 1 所示，损失服从式 (1)。[^1]

![图 1：训练曲线](images/curve.png)

$$L = \sum_i (y_i - \hat{y}_i)^2 \tag{1}$$

| 模型 | 层数 | 准确率 |
|-------|--------|----------|
| 小 | 12 | 70.5 |
| 大 | 24 | 80.1 |

: 表 1：留出集上的结果。

```python
loss = criterion(output, target)
```

细节见[方法](#method)与第 2 节。

[^1]: 推导遵循标准做法。
"""


def _blocks(markdown: str) -> list[dict]:
    return pandoc.markdown_to_ast(markdown)["blocks"]


def _audit(target: str = TARGET, source: str = SOURCE, **kwargs):
    source_blocks, target_blocks = _blocks(source), _blocks(target)
    return audit_document([AuditUnit(source_blocks, target_blocks, 7)], source_blocks, target_blocks, **kwargs)


def _translation_kinds(report) -> set[str]:
    return {finding.kind for finding in report.by_origin("translation")}


def test_faithful_translation_has_no_translation_issues():
    report = _audit()
    assert report.by_origin("translation") == []
    assert report.inventory["images"] == 1 and report.inventory["tables"] == 1 and report.inventory["footnotes"] == 1


@pytest.mark.parametrize(
    ("corrupt", "kind"),
    [
        (lambda t: t.replace("![图 1：训练曲线](images/curve.png)\n", ""), "image"),  # 丢图
        (lambda t: t.replace(r"(y_i - \hat{y}_i)^2", r"(y_i + \hat{y}_i)^2"), "math"),  # 改公式
        (lambda t: t.replace("| 80.1 |", "| 81.0 |"), "table"),  # 改表格数字
        (lambda t: t.replace("如图 1 和表 1", "如图 2 和表 1"), "reference"),  # 错编号
        (lambda t: t.replace("所示，损失服从式 (1)。[^1]", "所示，损失服从式 (1)。"), "footnote"),  # 断脚注
        (lambda t: t.replace("criterion(output, target)", "criterion(target, output)"), "code"),
        (lambda t: t.replace("[方法](#method)", "[方法](#methods)"), "link"),
    ],
    ids=["drop-image", "change-formula", "change-table-number", "wrong-number", "break-footnote", "code", "link"],
)
def test_injected_nontext_errors_are_detected(corrupt, kind):
    corrupted = corrupt(TARGET)
    assert corrupted != TARGET
    report = _audit(corrupted)
    assert kind in _translation_kinds(report)
    assert all(finding.seg_id == 7 for finding in report.by_origin("translation") if finding.kind == kind)


def test_table_shape_change_is_detected():
    corrupted = TARGET.replace("| 大 | 24 | 80.1 |\n", "")
    assert any("行列数" in f.message for f in _audit(corrupted).by_origin("translation"))


def test_reference_to_missing_figure_is_a_source_problem():
    source = SOURCE.replace("As shown in Figure 1", "As shown in Figure 4")
    target = TARGET.replace("如图 1", "如图 4")
    report = _audit(target, source)
    assert report.by_origin("translation") == []
    assert any(f.kind == "reference" and "图 4" in f.message for f in report.by_origin("source"))


def test_inconsistent_label_rendering_across_the_book():
    target = TARGET.replace("如图 1 和表 1", "如插图 1 和表 1").replace("第 2 节。", "第 2 节，另见图 1。")
    source = SOURCE.replace("Section 2 for details.", "Section 2 for details, also Figure 1.")
    report = _audit(target, source)
    assert any(f.kind == "label" and "「图」" in f.message and "「插图」" in f.message for f in report.findings)


def test_untranslated_caption_and_detached_caption():
    source = "Some text here.\n\n![](images/a.png)\n\nFigure 2: The old map of the desert.\n\nMore text follows.\n"
    detached = "一些文字。\n\n![](images/a.png)\n\n后面还有文字。\n\n图 2：沙漠的旧地图。\n"
    report = _audit(detached, source)
    assert any(f.kind == "caption" and "图 2" in f.message for f in report.by_origin("translation"))

    untranslated = _audit(
        "![The old map of the desert](images/a.png)",
        "![The old map of the desert](images/a.png)",
    )
    assert any(f.kind == "caption" for f in untranslated.by_origin("translation"))


def test_missing_image_file_is_reported_against_the_source():
    report = _audit(image_exists=lambda src: False)
    assert any(f.kind == "image" and "images/curve.png" in f.message for f in report.by_origin("source"))
    assert report.by_origin("translation") == []


def test_images_that_may_contain_english_are_only_flagged():
    report = _audit()
    notices = report.by_origin("notice")
    assert notices and all(f.kind == "image_text" and f.severity == "info" for f in notices)


def test_untranslated_chunk_only_gets_source_checks():
    source_blocks = _blocks(SOURCE.replace("Figure 1 and", "Figure 9 and"))
    unit = AuditUnit(source_blocks, source_blocks, 1, translated=False)
    report = audit_document([unit], source_blocks, source_blocks)
    assert report.by_origin("translation") == []
    assert any("图 9" in f.message for f in report.by_origin("source"))


def test_chinese_numerals_and_roman_chapters_match():
    source = "# Chapter III\n\nSee Chapter 3 and Section 2.1.\n\n# 2.1 Scope\n"
    target = "# 第三章\n\n见第三章与第 2.1 节。\n\n# 2.1 范围\n"
    assert _audit(target, source).by_origin("translation") == []


# --------------------------------------------------------------------------- #
# 导出流程：审计写盘、进入结构统计、进入对照阅读器的问题导航
# --------------------------------------------------------------------------- #

_BOOK = """# Results

Table 1 lists the scores, and Figure 1 shows the curve.

![Figure 1: The curve](images/curve.png)

| Model | Score |
|-------|-------|
| Small | 70 |

: Table 1: Scores.
"""


def _export_book(tmp_path, translate):
    import asyncio
    import json
    import re
    from dataclasses import asdict
    from pathlib import Path

    from langchain_core.runnables import RunnableLambda

    from src.core.translator import TranslationEngine
    from src.infrastructure.cache.translation_cache import TranslationCache
    from src.pipelines.structure.exporter import StructuredExporter
    from src.utils.config_loader import get_config

    def translator(prompt_value):
        body = prompt_value.to_string().split("【待翻译段落】:")[-1]
        segments = re.findall(r'<seg id="(\d+)">(.*?)</seg>', body, re.S)
        return "\n".join(f'<seg id="{i}">{translate(text)}</seg>' for i, text in segments)

    class Factory:
        def create_translator(self):
            return RunnableLambda(translator)

        def create_checker(self):
            return RunnableLambda(lambda prompt: "")

        def create_analyst(self):
            return RunnableLambda(lambda prompt: '{"summary": "测试"}')

    config = get_config(Path("config/config.yaml"))
    engine = TranslationEngine(
        model_factory=Factory(), cache=TranslationCache(tmp_path / "cache.db"), config=config
    )
    chunks = engine.plan_chunks(_BOOK)
    results = asyncio.run(engine.translate_batch(_BOOK, tmp_path / "stream.md", prepared_chunks=chunks))
    out = tmp_path / "out"
    stats = StructuredExporter.from_config(config).export_all(
        _BOOK, [asdict(r) for r in results], out, "doc", "测试", image_exists=lambda src: True
    )
    return stats, json.loads((out / "doc.audit.json").read_text(encoding="utf-8")), out


def test_export_writes_audit_and_marks_reader(tmp_path):
    def translate(text):
        return "译:" + text.replace("Table 1 lists", "Table 2 lists")

    stats, audit, out = _export_book(tmp_path, translate)
    assert stats["nontext_audit"]["translation_issues"] >= 1
    assert any(f["kind"] == "reference" and f["origin"] == "translation" for f in audit["findings"])
    reader = (out / "doc.html").read_text(encoding="utf-8")
    assert "非文字审计：编号引用对不上" in reader


def test_export_of_faithful_translation_has_no_audit_issues(tmp_path):
    stats, audit, _ = _export_book(tmp_path, lambda text: "译:" + text)
    assert stats["nontext_audit"]["translation_issues"] == 0
    assert audit["inventory"]["tables"] == 1 and audit["inventory"]["numbered_figures"] == 1


def test_block_order_change_is_reported_but_inline_reordering_is_not():
    source = "Text.\n\n![](a.png)\n\n```\nx = 1\n```\n"
    swapped = "文字。\n\n```\nx = 1\n```\n\n![](a.png)\n"
    assert any(f.kind == "order" for f in _audit(swapped, source).by_origin("translation"))
    inline_source = "Call `load(path)` before `evaluate(model)`."
    inline_target = "在调用 `evaluate(model)` 之前先调用 `load(path)`。"
    assert _audit(inline_target, inline_source).by_origin("translation") == []


def test_raw_html_endnote_markers_are_checked():
    source = "The oil speaks.<sup>12</sup> It returns.<sup>13</sup>\n"
    target = "石油在说话。<sup>12</sup>它又回来了。\n"
    assert any(f.kind == "footnote" and "13" in f.message for f in _audit(target, source).by_origin("translation"))
    assert _audit("石油在说话。<sup>12</sup>它又回来了。<sup>13</sup>\n", source).by_origin("translation") == []
