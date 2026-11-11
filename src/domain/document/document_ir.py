"""
文档中间表示（Document IR）

以 pandoc JSON AST 作为唯一事实来源：
- 结构（标题层级与锚点、列表、表格、图片、公式、代码、脚注）由 AST 持有；
- 可翻译的行内文字被抽取为"段"（Segment），经行内标签编码后交给 LLM；
- 译文按段回填到源 AST 的深拷贝中，从而保证结构与原文逐一对应。

术语：
- 候选（candidate）：AST 中任意一个"行内节点列表容器"（段落、标题文字、表格单元、
  脚注正文、图注……），按确定的深度优先顺序编号；
- 段（Segment）：含可翻译文字的候选，携带全局编号 seg_id；
- 单元（Unit）：切块的最小结构单位，通常是一个顶层块；超长容器块会下钻为子块；
- 块（Chunk）：若干连续单元，是一次 LLM 调用、缓存与人工审校的粒度。

本模块只做纯数据变换，不调用 pandoc 进程。
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from . import inline_codec

_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


# --------------------------------------------------------------------------- #
# AST 遍历
# --------------------------------------------------------------------------- #


def caption_blocks(caption) -> list[dict]:
    # Caption = [ShortCaption | null, [Block]]
    return caption[1] if caption else []


def child_block_lists(block: dict) -> list[list[dict]]:
    """返回容器块直接包含的块列表（用于单元下钻）。"""
    kind = block["t"]
    content = block.get("c")
    if kind in {"BlockQuote"}:
        return [content]
    if kind == "Div":
        return [content[1]]
    if kind == "BulletList":
        return list(content)
    if kind == "OrderedList":
        return list(content[1])
    return []


def iter_candidates(blocks: list[dict]) -> Iterator[list[dict]]:
    """按确定顺序产出所有行内列表容器（就地引用，可直接修改）。

    顺序约定：块内先产出自身行内列表，再递归其中脚注正文，再进入子块。
    源文与译文 AST 结构一致时，两者的候选序列一一对应。
    """
    for block in blocks:
        yield from _iter_block(block)


def _iter_inlines(inlines: list[dict]) -> Iterator[list[dict]]:
    yield inlines
    for note in _notes_in(inlines):
        yield from iter_candidates(note["c"])


def _notes_in(inlines: list[dict]) -> list[dict]:
    found: list[dict] = []

    def walk(nodes):
        for node in nodes:
            kind = node["t"]
            if kind == "Note":
                found.append(node)
            elif kind in inline_codec.PAIR_TAGS:
                walk(inline_codec.pair_children(node))
            elif kind == "Image":
                walk(node["c"][1])

    walk(inlines)
    return found


def _iter_block(block: dict) -> Iterator[list[dict]]:
    kind = block["t"]
    content = block.get("c")
    if kind in {"Para", "Plain"}:
        yield from _iter_inlines(content)
    elif kind == "Header":
        yield from _iter_inlines(content[2])
    elif kind == "LineBlock":
        for line in content:
            yield from _iter_inlines(line)
    elif kind == "BlockQuote":
        yield from iter_candidates(content)
    elif kind == "Div":
        yield from iter_candidates(content[1])
    elif kind == "BulletList":
        for item in content:
            yield from iter_candidates(item)
    elif kind == "OrderedList":
        for item in content[1]:
            yield from iter_candidates(item)
    elif kind == "DefinitionList":
        for term, definitions in content:
            yield from _iter_inlines(term)
            for definition in definitions:
                yield from iter_candidates(definition)
    elif kind == "Figure":
        yield from iter_candidates(caption_blocks(content[1]))
        yield from iter_candidates(content[2])
    elif kind == "Table":
        _, caption, _, head, bodies, foot = content
        yield from iter_candidates(caption_blocks(caption))
        for row in head[1]:
            yield from _iter_row(row)
        for body in bodies:
            for row in body[2]:
                yield from _iter_row(row)
            for row in body[3]:
                yield from _iter_row(row)
        for row in foot[1]:
            yield from _iter_row(row)
    # CodeBlock / RawBlock / HorizontalRule：无可翻译文字


def _iter_row(row) -> Iterator[list[dict]]:
    for cell in row[1]:
        yield from iter_candidates(cell[4])


def is_translatable(inlines: list[dict]) -> bool:
    return bool(_LETTER_RE.search(inline_codec.plain_text(inlines)))


def section_title_of(block: dict) -> str | None:
    if block["t"] == "Header":
        return inline_codec.plain_text(block["c"][2])
    return None


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


@dataclass
class Segment:
    """可翻译段。local_id 为块内编号（送给 LLM），seg_id 为全书编号（对照跳转）。"""

    seg_id: int
    local_id: int
    candidate_index: int  # 在所属块候选序列中的位置
    tagged: str
    plain: str
    in_note: bool = False


@dataclass
class Unit:
    path: tuple[tuple[int, int], ...]  # ((child_list_index, block_index), ...)，首项 child_list_index 恒为 -1
    block: dict
    section_path: list[str]
    verbatim: bool = False  # 参考文献等保留原文的单元：不送模型、原样输出


@dataclass
class StructuredChunk:
    """块的结构化载荷：所属单元 + 段。"""

    index: int
    units: list[Unit]
    segments: list[Segment]
    candidate_count: int
    first_seg_id: int
    pandoc_api: list = field(default_factory=list)

    @property
    def blocks(self) -> list[dict]:
        return [unit.block for unit in self.units]

    def tagged_payload(self) -> str:
        return "\n".join(f'<seg id="{seg.local_id}">{seg.tagged}</seg>' for seg in self.segments)


@dataclass
class DocumentIR:
    ast: dict
    units: list[Unit] = field(default_factory=list)

    @property
    def blocks(self) -> list[dict]:
        return self.ast["blocks"]


# --------------------------------------------------------------------------- #
# 单元与块规划
# --------------------------------------------------------------------------- #


def _unit_cost(block: dict) -> int:
    return sum(
        len(inline_codec.encode(candidate).text) for candidate in iter_candidates([block]) if is_translatable(candidate)
    )


VERBATIM_SECTION_RE = re.compile(
    r"\b(?:works\s+cited|bibliography|references|further\s+reading|select\s+bibliography)\b|参考文献|引用文献",
    re.IGNORECASE,
)


def plan_units(ast: dict, max_unit_cost: int, keep_bibliography: bool = False) -> list[Unit]:
    """把 AST 切成单元；超长容器（Div/引用/列表）下钻为子块。

    keep_bibliography：参考文献类标题（Works Cited、Bibliography……）之下、直到下一个同级或更高级
    标题为止的内容保留原文——书名、出版社、地名混译只会让条目难以检索。标题本身照常翻译。
    """
    units: list[Unit] = []
    heading_stack: list[tuple[int, str]] = []
    verbatim_level: list[int | None] = [None]

    def visit(blocks: list[dict], prefix: tuple, list_index: int):
        for block_index, block in enumerate(blocks):
            path = prefix + ((list_index, block_index),)
            children = child_block_lists(block)
            if children and block["t"] in {"Div", "BlockQuote"} and _unit_cost(block) > max_unit_cost:
                for child_index, child_blocks in enumerate(children):
                    visit(child_blocks, path, child_index)
                continue
            verbatim = False
            if block["t"] == "Header":
                level = block["c"][0]
                title = section_title_of(block) or ""
                heading_stack[:] = [item for item in heading_stack if item[0] < level]
                heading_stack.append((level, title))
                if verbatim_level[0] is not None and level <= verbatim_level[0]:
                    verbatim_level[0] = None
                if keep_bibliography and VERBATIM_SECTION_RE.search(title):
                    verbatim_level[0] = level
            else:
                verbatim = verbatim_level[0] is not None
            units.append(
                Unit(
                    path=path,
                    block=block,
                    section_path=[title for _, title in heading_stack],
                    verbatim=verbatim,
                )
            )

    visit(ast["blocks"], (), -1)
    return units


def plan_chunks(ir: DocumentIR, chunk_size: int, min_chunk_size: int) -> list[StructuredChunk]:
    """按章节与长度把单元打包成块，并编号段。"""
    groups: list[list[Unit]] = []
    current: list[Unit] = []
    current_cost = 0
    for unit in ir.units:
        cost = 0 if unit.verbatim else _unit_cost(unit.block)
        is_major_heading = unit.block["t"] == "Header" and unit.block["c"][0] <= 2
        if current and (
            (current_cost + cost > chunk_size and current_cost > 0)
            or (is_major_heading and current_cost >= min_chunk_size)
        ):
            groups.append(current)
            current, current_cost = [], 0
        current.append(unit)
        current_cost += cost
    if current:
        groups.append(current)

    chunks: list[StructuredChunk] = []
    next_seg_id = 0
    for index, units in enumerate(groups):
        segments: list[Segment] = []
        candidates = list(_candidates_with_note_flag([unit.block for unit in units]))
        verbatim_flags = [unit.verbatim for unit in units for _ in iter_candidates([unit.block])]
        for candidate_index, (inlines, in_note) in enumerate(candidates):
            if verbatim_flags[candidate_index] or not is_translatable(inlines):
                continue
            segments.append(
                Segment(
                    seg_id=next_seg_id,
                    local_id=len(segments) + 1,
                    candidate_index=candidate_index,
                    tagged=inline_codec.encode(inlines).text,
                    plain=inline_codec.plain_text(inlines),
                    in_note=in_note,
                )
            )
            next_seg_id += 1
        chunks.append(
            StructuredChunk(
                index=index,
                units=units,
                segments=segments,
                candidate_count=len(candidates),
                first_seg_id=segments[0].seg_id if segments else next_seg_id,
            )
        )
    return chunks


def _candidates_with_note_flag(blocks: list[dict]) -> Iterator[tuple[list[dict], bool]]:
    note_ids: set[int] = set()
    for inlines in iter_candidates(blocks):
        yield inlines, id(inlines) in note_ids
        for note in _notes_in(inlines):
            for inner in iter_candidates(note["c"]):
                note_ids.add(id(inner))


candidates_with_note_flag = _candidates_with_note_flag


def chunk_fingerprint(chunk: StructuredChunk) -> str:
    return hashlib.sha1(chunk.tagged_payload().encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# 回填
# --------------------------------------------------------------------------- #


def translate_blocks(blocks: list[dict], replacements: dict[int, list[dict] | str]) -> list[dict]:
    """返回 blocks 的深拷贝，其中第 i 个候选替换为译文。

    replacements: 候选序号 → 译文行内列表，或带标签译文字符串（以源候选解码）。
    逆序替换：脚注正文（排在所属段落之后）先被替换，随后段落解码时
    `<fn/>` 标签取到的正是已替换为译文的脚注节点。
    """
    clone = copy.deepcopy(blocks)
    candidates = list(iter_candidates(clone))
    for index in sorted(replacements, reverse=True):
        target = candidates[index]
        value = replacements[index]
        if isinstance(value, str):
            value = inline_codec.decode(value, target).inlines
        target[:] = value
    sync_figure_alts(clone)
    return clone


def sync_figure_alts(blocks: list[dict]) -> None:
    """图注译成中文后，把图片 alt 同步为图注，使其仍可写成 `![图注](src)` 的隐式图。"""

    def walk(value):
        if isinstance(value, dict):
            if value.get("t") == "Figure":
                caption = caption_blocks(value["c"][1])
                body = value["c"][2]
                if (
                    len(caption) == 1
                    and caption[0]["t"] in {"Plain", "Para"}
                    and len(body) == 1
                    and body[0]["t"] in {"Plain", "Para"}
                    and len(body[0]["c"]) == 1
                    and body[0]["c"][0]["t"] == "Image"
                ):
                    body[0]["c"][0]["c"][1] = copy.deepcopy(caption[0]["c"])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(blocks)


def translated_candidates(source_blocks: list[dict], translated_blocks: list[dict]) -> list[list[dict]] | None:
    """若译文块与源块结构一致（候选数与块类型序列相同），返回译文候选序列。"""
    if [block["t"] for block in source_blocks] != [block["t"] for block in translated_blocks]:
        return None
    source = list(iter_candidates(source_blocks))
    target = list(iter_candidates(translated_blocks))
    if len(source) != len(target):
        return None
    return target


def top_level_candidate_indices(blocks: list[dict]) -> list[int]:
    """非脚注内部的候选序号（回填时只需替换这些，脚注随段落整体带入）。"""
    return [index for index, (_, in_note) in enumerate(_candidates_with_note_flag(blocks)) if not in_note]


def set_block_at(blocks: list[dict], path: tuple[tuple[int, int], ...], new_blocks: list[dict]) -> None:
    """把 path 指向的块替换为 new_blocks（可为多个块）。调用方需逆序处理以免下标漂移。"""
    container = blocks
    for depth, (_, block_index) in enumerate(path):
        if depth == len(path) - 1:
            container[block_index : block_index + 1] = new_blocks
            return
        next_list_index = path[depth + 1][0]
        container = child_block_lists(container[block_index])[next_list_index]


def get_block_at(blocks: list[dict], path: tuple[tuple[int, int], ...]) -> dict:
    container = blocks
    for depth, (_, block_index) in enumerate(path):
        block = container[block_index]
        if depth == len(path) - 1:
            return block
        container = child_block_lists(block)[path[depth + 1][0]]
    raise ValueError("空路径")


# --------------------------------------------------------------------------- #
# 段 ID 标注（双栏 HTML 联动）
# --------------------------------------------------------------------------- #


def wrap_segments(
    blocks: list[dict],
    seg_ids: dict[int, int],
    classes: dict[int, list[str]] | None = None,
    attrs: dict[int, list[list[str]]] | None = None,
) -> None:
    """就地把候选的行内内容包进 `Span(class=seg, data-seg=N)`，不改变块结构。"""
    classes = classes or {}
    attrs = attrs or {}
    for index, inlines in enumerate(list(iter_candidates(blocks))):
        if index not in seg_ids:
            continue
        seg_id = seg_ids[index]
        span = {
            "t": "Span",
            "c": [
                ["", ["seg", *classes.get(index, [])], [["seg", str(seg_id)], *attrs.get(index, [])]],
                list(inlines),
            ],
        }
        inlines[:] = [span]


def strip_identifiers(blocks: list[dict]) -> list[dict]:
    """深拷贝并清除标题/Div/Span 的 id，避免对照文档中出现重复锚点。"""
    clone = copy.deepcopy(blocks)

    def walk(value):
        if isinstance(value, dict):
            kind = value.get("t")
            if kind == "Header":
                value["c"][1][0] = ""
            elif kind in {"Div", "Span"}:
                value["c"][0][0] = ""
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(clone)
    return clone


def collect_link_report(blocks: list[dict]) -> dict:
    """统计内部锚点链接是否都能解析到 id（死链检查）。"""
    ids: set[str] = set()
    targets: list[str] = []

    def walk(value):
        if isinstance(value, dict):
            kind = value.get("t")
            content = value.get("c")
            if kind == "Header":
                ids.add(content[1][0])
            elif kind in {"Div", "Span", "CodeBlock", "Table", "Figure"}:
                ids.add(content[0][0])
            elif kind in {"Link", "Image"}:
                if content[0][0]:
                    ids.add(content[0][0])
                url = content[2][0]
                if url.startswith("#"):
                    targets.append(url[1:])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(blocks)
    ids.discard("")
    dead = sorted({target for target in targets if target not in ids})
    return {"internal_links": len(targets), "dead_links": dead}
