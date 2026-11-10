"""
尾注链接化

很多 EPUB（尤其 Calibre 转出的）把注释做成"正文上标数字 + 章末 Notes 有序列表"，两者之间没有链接，
pandoc 也就读不出脚注。这里在导出时补上双向链接：上标 → 对应注释，注释 → 回到引用处。

位置在原文 AST 上识别（"Notes" 标题不受翻译措辞影响），再按相同结构应用到原文与译文。
只处理有明确证据的情况：注释标题后紧跟有序列表，且上标数字落在列表编号范围内。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ...domain.document import document_ir, inline_codec

# 以 Notes/Footnotes 结尾的标题（"Notes"、"Endnotes"、Ccru 的 "Swollen Footnotes"）或中文"注释/尾注"；
# 另要求标题后紧跟有序列表，单凭标题不会误判
NOTES_HEADING_RE = re.compile(r"(?:^|\s)(?:foot|end\s*)?notes\s*$|^\s*(?:注释|注|尾注|脚注)\s*$", re.IGNORECASE)
_NUMBER_RE = re.compile(r"^\d{1,3}$")


@dataclass
class EndnoteSection:
    container: tuple[tuple[int, int], ...]  # 块列表所在位置：((block_index, child_list_index), ...)
    scope_start: int  # 引用范围起点（父章节标题之后）
    heading_index: int  # 注释标题位置
    list_indices: list[int]  # 注释有序列表位置（注释中夹有引文段落时会被拆成多个列表）


def _containers(blocks: list[dict], path: tuple = ()):
    yield path, blocks
    for index, block in enumerate(blocks):
        if block["t"] in {"Div", "BlockQuote"}:
            for child_index, child in enumerate(document_ir.child_block_lists(block)):
                yield from _containers(child, path + ((index, child_index),))


def _resolve(blocks: list[dict], path: tuple) -> list[dict]:
    for index, child_index in path:
        blocks = document_ir.child_block_lists(blocks[index])[child_index]
    return blocks


def _is_anchor_only(block: dict) -> bool:
    return block["t"] in {"Para", "Plain"} and all(
        node["t"] in {"Space", "SoftBreak"} or (node["t"] == "Span" and not node["c"][1]) for node in block["c"]
    )


def find_endnote_sections(blocks: list[dict]) -> list[EndnoteSection]:
    """在原文中识别"注释标题 + 有序列表"，并确定各自的引用范围。"""
    sections: list[EndnoteSection] = []
    for path, container in _containers(blocks):
        previous_end = 0
        for index, block in enumerate(container):
            if block["t"] != "Header" or not NOTES_HEADING_RE.search(document_ir.section_title_of(block) or ""):
                continue
            first = index + 1
            while first < len(container) and _is_anchor_only(container[first]):
                first += 1
            if first >= len(container) or container[first]["t"] != "OrderedList":
                continue
            end = first
            while end < len(container) and container[end]["t"] != "Header":
                end += 1
            list_indices = [i for i in range(first, end) if container[i]["t"] == "OrderedList"]
            level = block["c"][0]
            scope_start = previous_end
            for back in range(index - 1, previous_end - 1, -1):
                candidate = container[back]
                if candidate["t"] == "Header" and candidate["c"][0] < level:
                    scope_start = back
                    break
            sections.append(EndnoteSection(path, scope_start, index, list_indices))
            previous_end = end
    return sections


def _superscripts(value, found: list[dict]):
    if isinstance(value, dict):
        if value.get("t") == "Superscript":
            found.append(value)
            return
        for item in value.values():
            _superscripts(item, found)
    elif isinstance(value, list):
        for item in value:
            _superscripts(item, found)


def link_endnotes(blocks: list[dict], sections: list[EndnoteSection]) -> int:
    """就地为每个尾注区段加上双向链接，返回链接上的引用数。"""
    linked = 0
    for number, section in enumerate(sections, 1):
        try:
            container = _resolve(blocks, section.container)
        except (IndexError, KeyError):
            continue
        # 译文中人工审校改动了块结构时，位置可能对不上：只在结构仍一致时处理
        if (
            section.heading_index >= len(container)
            or container[section.heading_index]["t"] != "Header"
            or any(index >= len(container) or container[index]["t"] != "OrderedList" for index in section.list_indices)
        ):
            continue
        items: dict[int, list[dict]] = {}
        for list_index in section.list_indices:
            start = container[list_index]["c"][0][0]
            for offset, item in enumerate(container[list_index]["c"][1]):
                items.setdefault(start + offset, item)
        references: list[dict] = []
        _superscripts(container[section.scope_start : section.heading_index], references)
        anchored: set[int] = set()
        for sup in references:
            text = inline_codec.plain_text(sup["c"])
            if not _NUMBER_RE.match(text) or int(text) not in items:
                continue
            n = int(text)
            ref_id = f"endnote-{number}-ref-{n}" if n not in anchored else ""
            anchored.add(n)
            sup["c"] = [
                {
                    "t": "Link",
                    "c": [[ref_id, ["endnote-ref"], []], [{"t": "Str", "c": text}], [f"#endnote-{number}-{n}", ""]],
                }
            ]
            linked += 1
        for n in anchored:
            item = items[n]
            if not item or item[0]["t"] not in {"Plain", "Para"}:
                continue
            first = item[0]["c"]
            first.insert(0, {"t": "Span", "c": [[f"endnote-{number}-{n}", ["endnote"], []], []]})
            first.extend(
                [
                    {"t": "Space"},
                    {
                        "t": "Link",
                        "c": [
                            ["", ["endnote-back"], []],
                            [{"t": "Str", "c": "↩"}],
                            [f"#endnote-{number}-ref-{n}", ""],
                        ],
                    },
                ]
            )
    return linked
