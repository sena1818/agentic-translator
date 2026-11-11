"""
结构化导出：从块级译文回填完整文档，产出单语 Markdown、段级双语 Markdown、
以及可双向跳转的双栏 HTML 对照阅读器。

输入只依赖"源 Markdown + 块级翻译记录"，因此翻译完成、人工审校编辑后都能重渲染。
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ...domain.document import document_ir, inline_codec
from ...infrastructure.document import pandoc
from .delivery import ImageInliner, ImageLocator, resolver_from_locator, write_epub
from .endnotes import find_endnote_sections, link_endnotes
from .html_template import render_reader_html
from .planner import StructuredDocumentPlanner, StructuredPlan

logger = logging.getLogger(__name__)


@dataclass
class ChunkAssembly:
    structured: document_ir.StructuredChunk
    status: str  # aligned | unaligned | failed
    translated_blocks: list[dict]
    issues: dict[int, list[str]] = field(default_factory=dict)  # seg_id → 问题描述


@dataclass
class AssembledDocument:
    plan: StructuredPlan
    chunks: list[ChunkAssembly]
    translated_ast: dict
    link_report: dict
    endnote_sections: list = field(default_factory=list)

    @property
    def stats(self) -> dict:
        segments = sum(len(item.structured.segments) for item in self.chunks)
        return {
            "segments": segments,
            "chunks": len(self.chunks),
            "aligned_chunks": sum(item.status == "aligned" for item in self.chunks),
            "unaligned_chunks": sum(item.status == "unaligned" for item in self.chunks),
            "failed_chunks": sum(item.status == "failed" for item in self.chunks),
            "segments_with_issues": sum(len(item.issues) for item in self.chunks),
            "internal_links": self.link_report["internal_links"],
            "dead_links": self.link_report["dead_links"],
        }


def _record(item) -> dict:
    return item if isinstance(item, dict) else item.__dict__


class StructuredExporter:
    def __init__(self, planner: StructuredDocumentPlanner):
        self.planner = planner

    @classmethod
    def from_config(cls, config) -> StructuredExporter:
        return cls(StructuredDocumentPlanner.from_config(config))

    # ------------------------------------------------------------------ #
    # 回填
    # ------------------------------------------------------------------ #

    def assemble(self, source_text: str, records: list) -> AssembledDocument | None:
        """按块回填译文。块划分与记录不一致（源文或参数变化）时返回 None，由调用方回落旧导出。"""
        plan = self.planner.plan(source_text)
        by_index = {_record(item)["chunk_index"]: _record(item) for item in records}
        if len(by_index) != len(plan.chunks) or any(
            by_index.get(chunk.index, {}).get("chunk_id") not in {chunk.chunk_id, ""} for chunk in plan.chunks
        ):
            logger.warning("翻译记录与结构化规划不一致，跳过结构化导出")
            return None

        successful = [chunk for chunk in plan.chunks if by_index[chunk.index].get("success")]
        parsed = pandoc.markdown_groups_to_blocks([by_index[chunk.index]["translation"] for chunk in successful])
        parsed_by_index = {chunk.index: blocks for chunk, blocks in zip(successful, parsed, strict=True)}

        assemblies: list[ChunkAssembly] = []
        for chunk in plan.chunks:
            structured: document_ir.StructuredChunk = chunk.structured
            record = by_index[chunk.index]
            issues = _segment_issues(record.get("quality_report") or {})
            if chunk.index not in parsed_by_index:
                assemblies.append(
                    ChunkAssembly(
                        structured,
                        "failed",
                        copy.deepcopy(structured.blocks),
                        {seg.seg_id: ["翻译失败，保留原文"] for seg in structured.segments},
                    )
                )
                continue
            translated = parsed_by_index[chunk.index]
            candidates = document_ir.translated_candidates(structured.blocks, translated)
            if candidates is None:
                assemblies.append(ChunkAssembly(structured, "unaligned", translated, issues))
                continue
            replacements = {
                index: candidates[index] for index in document_ir.top_level_candidate_indices(structured.blocks)
            }
            assemblies.append(
                ChunkAssembly(
                    structured, "aligned", document_ir.translate_blocks(structured.blocks, replacements), issues
                )
            )

        translated_ast = copy.deepcopy(plan.ir.ast)
        _apply_units(translated_ast["blocks"], assemblies, lambda item: item.translated_blocks)
        # 块对象与 translated_ast 共享，原地改写即可让 HTML、双语导出一并生效
        sync_toc_entries(translated_ast["blocks"])
        dedupe_glosses(translated_ast["blocks"])
        normalize_cjk_quotes(translated_ast["blocks"])
        endnote_sections = find_endnote_sections(plan.ir.ast["blocks"])
        link_endnotes(translated_ast["blocks"], endnote_sections)
        return AssembledDocument(
            plan=plan,
            chunks=assemblies,
            translated_ast=translated_ast,
            link_report=document_ir.collect_link_report(translated_ast["blocks"]),
            endnote_sections=endnote_sections,
        )

    # ------------------------------------------------------------------ #
    # 输出
    # ------------------------------------------------------------------ #

    @staticmethod
    def mono_markdown(document: AssembledDocument) -> str:
        return pandoc.document_to_markdown(document.translated_ast) + "\n"

    @staticmethod
    def bilingual_markdown(document: AssembledDocument) -> str:
        """段级（单元级）对照：每个含文字的单元先引用原文，紧跟译文。"""
        return pandoc.blocks_to_markdown(document.translated_ast, StructuredExporter.bilingual_blocks(document)) + "\n"

    @staticmethod
    def bilingual_blocks(document: AssembledDocument) -> list[dict]:
        blocks: list[dict] = []
        for item in document.chunks:
            if item.status == "aligned":
                for unit, translated in zip(item.structured.units, item.translated_blocks, strict=True):
                    if any(document_ir.is_translatable(c) for c in document_ir.iter_candidates([unit.block])):
                        blocks.append({"t": "BlockQuote", "c": [_quote_form(unit.block)]})
                    blocks.append(translated)
            else:
                # 结构被改动（人工审校改了段落划分）或翻译失败：退化为块级对照
                blocks.append({"t": "BlockQuote", "c": [_quote_form(unit.block) for unit in item.structured.units]})
                blocks.extend(item.translated_blocks)
                blocks.append({"t": "HorizontalRule"})
        return blocks

    @staticmethod
    def reader_html(
        document: AssembledDocument,
        title: str,
        image_resolver: Callable[[str], str] | None = None,
    ) -> str:
        """双栏对照阅读器：段级双向跳转、悬停联动、跟随滚动、目录、脚注浮层、问题段导航。"""
        return StructuredExporter.render_reader(document, title, image_resolver)[0]

    @staticmethod
    def render_reader(
        document: AssembledDocument,
        title: str,
        image_resolver: Callable[[str], str] | None = None,
    ) -> tuple[str, list[str]]:
        """返回 (阅读器 HTML, 找不到的图片)。内联图片两栏共用一份。"""
        source_ast = copy.deepcopy(document.plan.ir.ast)
        target_ast = copy.deepcopy(document.plan.ir.ast)

        def wrapped_source(item: ChunkAssembly) -> list[dict]:
            blocks = copy.deepcopy(item.structured.blocks)
            document_ir.wrap_segments(blocks, _seg_map(item.structured))
            return blocks

        def wrapped_target(item: ChunkAssembly) -> list[dict]:
            blocks = copy.deepcopy(item.translated_blocks)
            if item.status == "unaligned":
                return blocks
            classes, attrs = {}, {}
            for seg in item.structured.segments:
                messages = item.issues.get(seg.seg_id)
                if messages:
                    classes[seg.candidate_index] = ["seg-failed" if item.status == "failed" else "seg-issue"]
                    attrs[seg.candidate_index] = [["issue", " / ".join(messages)[:500]]]
            document_ir.wrap_segments(blocks, _seg_map(item.structured), classes, attrs)
            return blocks

        _apply_units(source_ast["blocks"], document.chunks, wrapped_source)
        _apply_units(target_ast["blocks"], document.chunks, wrapped_target)
        for ast in (source_ast, target_ast):
            link_endnotes(ast["blocks"], document.endnote_sections)
        for ast in (source_ast, target_ast):
            _drop_unsafe_raw(ast["blocks"])
        source_html = pandoc.blocks_to_html(source_ast, source_ast["blocks"], "src-")
        target_html = pandoc.blocks_to_html(target_ast, target_ast["blocks"], "tgt-")
        inliner = None
        if image_resolver is not None:
            inliner = ImageInliner(image_resolver)
            source_html = inliner.rewrite(source_html)
            target_html = inliner.rewrite(target_html)
        missing = inliner.missing if inliner else []

        page = render_reader_html(
            title=title,
            source_html=source_html,
            target_html=target_html,
            toc=_toc(document),
            stats={**document.stats, "missing_images": missing},
            images=inliner.images if inliner else None,
        )
        return page, missing

    def export_all(
        self,
        source_text: str,
        records: list,
        output_dir: Path,
        stem: str,
        title: str,
        bilingual: bool = True,
        image_resolver: Callable[[str], str] | None = None,
        image_locator: ImageLocator | None = None,
        epub: bool = True,
    ) -> dict | None:
        """写出 `{stem}.md`、`{stem}.bilingual.md`（可选）、`{stem}.html`、EPUB 与结构统计；不可用时返回 None。

        image_locator 把文档里的图片路径解析为本地文件：阅读器据此内联图片，EPUB 据此打包图片。
        统计里的 missing_images 列出两者都找不到的图片。
        """
        document = self.assemble(source_text, records)
        if document is None:
            return None
        if image_resolver is None and image_locator is not None:
            image_resolver = resolver_from_locator(image_locator)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / f"{stem}.md").write_text(self.mono_markdown(document), encoding="utf-8")
        if bilingual:
            (output_dir / f"{stem}.bilingual.md").write_text(self.bilingual_markdown(document), encoding="utf-8")
        page, missing = self.render_reader(document, title, image_resolver)
        (output_dir / f"{stem}.html").write_text(page, encoding="utf-8")
        stats = {**document.stats, "missing_images": missing, "epub": []}
        if epub:
            stats["epub"] = self._write_epubs(document, output_dir, stem, title, bilingual, image_locator, missing)
        (output_dir / f"{stem}.structure.json").write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return stats

    def _write_epubs(self, document, output_dir, stem, title, bilingual, locator, missing) -> list[str]:
        """仅译文与双语两种 EPUB；生成失败只记日志，不影响其他导出。返回成功写出的变体。"""
        variants = [("mono", f"{stem}.epub", document.translated_ast["blocks"])]
        if bilingual:
            variants.append(("bilingual", f"{stem}.bilingual.epub", self.bilingual_blocks(document)))
        written = []
        for name, filename, blocks in variants:
            try:
                lost = write_epub(
                    document.translated_ast, blocks, output_dir / filename, title, locator, _drop_unsafe_raw
                )
            except Exception:
                logger.warning("EPUB 导出失败（%s）: %s", name, filename, exc_info=True)
                continue
            missing.extend(src for src in lost if src not in missing and not src.startswith(("http://", "https://")))
            written.append(name)
        return written


# ---------------------------------------------------------------------- #
# 工具函数
# ---------------------------------------------------------------------- #


def _seg_map(structured: document_ir.StructuredChunk) -> dict[int, int]:
    return {seg.candidate_index: seg.seg_id for seg in structured.segments}


def _segment_issues(report: dict) -> dict[int, list[str]]:
    issues: dict[int, list[str]] = {}
    for issue in report.get("issues") or []:
        if issue.get("segment") is None or issue.get("severity") == "low":
            continue
        issues.setdefault(int(issue["segment"]), []).append(re.sub(r"^\[段 \d+\]\s*", "", issue["message"]))
    return issues


def _apply_units(blocks: list[dict], assemblies: list[ChunkAssembly], choose) -> None:
    """把每个块的新块列表写回文档对应单元位置（逆序，避免下标漂移）。"""
    replacements: list[tuple[tuple, list[dict]]] = []
    for item in assemblies:
        new_blocks = choose(item)
        units = item.structured.units
        if item.status == "unaligned":
            replacements.append((units[0].path, new_blocks))
            replacements.extend((unit.path, []) for unit in units[1:])
        else:
            replacements.extend((unit.path, [block]) for unit, block in zip(units, new_blocks, strict=True))
    for path, new in sorted(replacements, key=lambda pair: pair[0], reverse=True):
        document_ir.set_block_at(blocks, path, new)


_GLOSS_RE = re.compile(
    r"(?P<zh>[\u3400-\u9fff][\u3400-\u9fff·・\-]*)\s*[（(]\s*(?P<en>[A-Za-z][A-Za-z0-9 .'’\-]*?)\s*[)）]"
    r"(?P<trail>\s*)(?=(?P<next>.?))",
    re.DOTALL,
)
_SELF_GLOSS_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z][\w.\-’']*)\s*[（(]\1[)）]")
_CJK_OR_PUNCT = re.compile(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef“”‘’—…]")


def dedupe_glosses(blocks: list[dict]) -> int:
    """英文括注只在正文首次出现时保留：`超虚构 (Hyperstition)` 第二次起写成 `超虚构`。

    标题（及随后同步的目录）一律不带括注且不计为首次出现；不依赖术语表，
    模型自行添加的括注（如 `逃逸线（line of flight）`）同样去重。返回去掉的括注数。
    """
    seen: set[str] = set()
    removed = 0
    toc_links = {id(link) for link in _toc_links(blocks)}

    def make_rewrite(in_heading: bool):
        def rewrite(text: str) -> str:
            def replace(match: re.Match[str]) -> str:
                nonlocal removed
                key = re.sub(r"\s+", " ", match.group("en")).strip().lower()
                if in_heading or key in seen:
                    removed += 1
                    # 括注两侧的空格只为隔开英文；去掉括注后若紧接中文/中文标点，空格一并去掉
                    keep_space = match.group("trail") and not _CJK_OR_PUNCT.match(match.group("next") or "。")
                    return match.group("zh") + (" " if keep_space else "")
                seen.add(key)
                return match.group(0)

            # 原文保留不译的名字被模型自我括注："Ccru (Ccru)" → "Ccru"
            text = _SELF_GLOSS_RE.sub(lambda match: match.group(1), text)
            return _GLOSS_RE.sub(replace, text)

        return rewrite

    def visit_inlines(inlines: list[dict], in_heading: bool):
        inline_codec.rewrite_text_runs(inlines, make_rewrite(in_heading))
        for node in inlines:
            kind = node["t"]
            if kind == "Note":
                visit_blocks(node["c"])
            elif kind in inline_codec.PAIR_TAGS:
                visit_inlines(inline_codec.pair_children(node), in_heading or id(node) in toc_links)

    def visit_blocks(items: list[dict]):
        for block in items:
            if block["t"] == "Header":
                visit_inlines(block["c"][2], True)
            elif block["t"] in {"Para", "Plain"}:
                visit_inlines(block["c"], False)
            else:
                for children in _child_lists(block):
                    visit_blocks(children)

    visit_blocks(blocks)
    return removed


_CJK_SINGLE_QUOTED = re.compile(r"‘([^‘’\n]*[\u3400-\u9fff][^‘’\n]*)’")


def normalize_cjk_quotes(blocks: list[dict]) -> int:
    """中文译文里沿用的英文单引号 ‘…’ 统一为中文双引号 “…”（只改内容含中文的引号）。"""
    changed = 0

    def rewrite(text: str) -> str:
        nonlocal changed
        new, count = _CJK_SINGLE_QUOTED.subn(r"“\1”", text)
        changed += count
        return new

    def visit_inlines(inlines: list[dict]):
        nonlocal changed
        inline_codec.rewrite_text_runs(inlines, rewrite)
        for node in inlines:
            if node["t"] == "Note":
                visit_blocks(node["c"])
                continue
            # pandoc 把成对的 ‘…’ 解析为 Quoted(SingleQuote) 节点，而不是文字
            if (
                node["t"] == "Quoted"
                and node["c"][0]["t"] == "SingleQuote"
                and re.search(r"[\u3400-\u9fff]", inline_codec.plain_text(node["c"][1]))
            ):
                node["c"][0] = {"t": "DoubleQuote"}
                changed += 1
            if node["t"] in inline_codec.PAIR_TAGS:
                visit_inlines(inline_codec.pair_children(node))

    def visit_blocks(items: list[dict]):
        for block in items:
            if block["t"] == "Header":
                visit_inlines(block["c"][2])
            elif block["t"] in {"Para", "Plain"}:
                visit_inlines(block["c"])
            else:
                for children in _child_lists(block):
                    visit_blocks(children)

    visit_blocks(blocks)
    return changed


def _child_lists(block: dict) -> list[list[dict]]:
    """容器块的子块列表（含表格单元、图注、定义列表），供按文档顺序遍历。"""
    kind, content = block["t"], block.get("c")
    if kind == "Table":
        _, caption, _, head, bodies, foot = content
        rows = list(head[1]) + [row for body in bodies for row in body[2] + body[3]] + list(foot[1])
        return [document_ir.caption_blocks(caption)] + [cell[4] for row in rows for cell in row[1]]
    if kind == "Figure":
        return [document_ir.caption_blocks(content[1]), content[2]]
    if kind == "DefinitionList":
        return [[{"t": "Plain", "c": term}] for term, _ in content] + [
            definition for _, definitions in content for definition in definitions
        ]
    if kind == "LineBlock":
        return [[{"t": "Plain", "c": line}] for line in content]
    return document_ir.child_block_lists(block)


def _header_inlines(blocks: list[dict]) -> dict[str, list[dict]]:
    headers: dict[str, list[dict]] = {}

    def collect(value):
        if isinstance(value, dict):
            if value.get("t") == "Header" and value["c"][1][0]:
                headers[value["c"][1][0]] = [node for node in value["c"][2] if node["t"] != "Note"]
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(blocks)
    return headers


def _toc_links(blocks: list[dict], headers: dict | None = None) -> list[dict]:
    """目录项里的链接：列表项仅含一个指向标题的内部链接。"""
    headers = _header_inlines(blocks) if headers is None else headers
    found: list[dict] = []

    def visit(value):
        if isinstance(value, dict):
            kind = value.get("t")
            items = value["c"] if kind == "BulletList" else value["c"][1] if kind == "OrderedList" else []
            for item in items:
                first = item[0] if item else None
                if not first or first["t"] not in {"Plain", "Para"}:
                    continue
                nodes = [node for node in first["c"] if node["t"] not in {"Space", "SoftBreak"}]
                if len(nodes) == 1 and nodes[0]["t"] == "Link":
                    target = nodes[0]["c"][2][0]
                    if target.startswith("#") and target[1:] in headers:
                        found.append(nodes[0])
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(blocks)
    return found


def sync_toc_entries(blocks: list[dict]) -> int:
    """目录项（列表项仅含一个指向标题的内部链接）的文字改为该标题的译文，返回同步条数。

    目录与标题分属不同块、分别翻译，措辞可能不一致；正文里的交叉引用（"见第三节"）保留自身措辞。
    """
    headers = _header_inlines(blocks)
    links = _toc_links(blocks, headers)
    for link in links:
        link["c"][1][:] = copy.deepcopy(headers[link["c"][2][0][1:]])
    return len(links)


def _quote_form(block: dict) -> dict:
    """引用块里的原文：去掉锚点 id；标题降为加粗段落，避免重复进入目录。"""
    clean = document_ir.strip_identifiers([block])[0]
    if clean["t"] == "Header":
        return {"t": "Para", "c": [{"t": "Strong", "c": clean["c"][2]}]}
    if clean["t"] == "Figure":  # 图片只在译文侧出现一次，原文侧只引用图注
        caption = [inline for block in document_ir.caption_blocks(clean["c"][1]) for inline in block.get("c", [])]
        return {"t": "Para", "c": [{"t": "Emph", "c": caption}]}
    return clean


def _toc(document: AssembledDocument) -> list[dict]:
    entries = []
    for item in document.chunks:
        if item.status == "unaligned":
            continue
        for unit, translated in zip(item.structured.units, item.translated_blocks, strict=True):
            if unit.block["t"] != "Header" or len(unit.path) > 1:
                continue
            seg = next(
                (s for s in item.structured.segments if s.candidate_index == _candidate_offset(item, unit)), None
            )
            if seg is None:
                continue
            entries.append(
                {
                    "level": unit.block["c"][0],
                    "title": inline_codec.plain_text(translated["c"][2]) or seg.plain,
                    "source": seg.plain,
                    "seg": seg.seg_id,
                }
            )
    return entries


def _candidate_offset(item: ChunkAssembly, target_unit) -> int:
    offset = 0
    for unit in item.structured.units:
        if unit is target_unit:
            return offset
        offset += sum(1 for _ in document_ir.iter_candidates([unit.block]))
    return -1


_UNSAFE_RAW = re.compile(r"<\s*(script|iframe|object|embed)|javascript:|\bon[a-z]+\s*=", re.IGNORECASE)


def _drop_unsafe_raw(value) -> None:
    """对照阅读器内嵌脚本运行，去掉源文档中可能携带脚本的原始 HTML。"""
    if isinstance(value, list):
        for index in range(len(value) - 1, -1, -1):
            item = value[index]
            if (
                isinstance(item, dict)
                and item.get("t") in {"RawBlock", "RawInline"}
                and _UNSAFE_RAW.search(item["c"][1])
            ):
                del value[index]
            else:
                _drop_unsafe_raw(item)
    elif isinstance(value, dict):
        for item in value.values():
            _drop_unsafe_raw(item)
