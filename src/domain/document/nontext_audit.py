"""
全书非文字审计（确定性，不调用模型）

在导出前对整本书比对原文与译文的非文字内容：
- 图片、公式、代码：数量、顺序、内容逐一对应；图片文件存在；
- 表格：行列数一致，单元格里的数字不变；
- 脚注与尾注：脚注个数、尾注上标编号、链接与跳转目标一致；
- 编号与交叉引用：正文里"图 3""式 (2)""表 1""第 2 节"能对上原文的编号，且能对上书里实际存在的对象；
  同类标签全书译法统一（"图"与"插图"混用会被报出）；
- 图注：已翻译，且仍与对应的图相邻；
- 图片内部文字不翻译，可能含英文的图片只做标记。

每条发现带来源（origin）：
- translation  翻译引入：原文对得上、译文对不上；
- source       原文本身就有：如引用了不存在的图、原文锚点死链、图片文件缺失；
- notice       只作提示：可能含英文的图片。

输入是 pandoc JSON AST 的块列表，本模块只做纯数据变换；"图片文件是否存在"由调用方注入判断函数。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from . import inline_codec

# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class AuditUnit:
    """一组对应的原文块与译文块；seg_id 是该组第一个段的全书编号，供对照阅读器定位。"""

    source: list[dict]
    target: list[dict]
    seg_id: int | None = None
    translated: bool = True  # 翻译失败、保留原文的块为 False，只做原文侧检查


@dataclass
class AuditFinding:
    kind: str  # image | math | code | table | footnote | link | reference | label | caption | image_text
    severity: str  # high | medium | low | info
    origin: str  # translation | source | notice
    message: str
    seg_id: int | None = None

    def as_dict(self) -> dict:
        data = {"kind": self.kind, "severity": self.severity, "origin": self.origin, "message": self.message}
        if self.seg_id is not None:
            data["segment"] = self.seg_id
        return data


@dataclass
class AuditReport:
    findings: list[AuditFinding] = field(default_factory=list)
    inventory: dict[str, int] = field(default_factory=dict)  # 原文中各类对象的数量

    def by_origin(self, origin: str) -> list[AuditFinding]:
        return [finding for finding in self.findings if finding.origin == origin]

    @property
    def summary(self) -> dict:
        counts: dict[str, dict[str, int]] = {}
        for finding in self.findings:
            counts.setdefault(finding.origin, Counter())[finding.kind] += 1
        return {
            "translation_issues": len(self.by_origin("translation")),
            "source_issues": len(self.by_origin("source")),
            "notices": len(self.by_origin("notice")),
            "by_origin": {origin: dict(kinds) for origin, kinds in counts.items()},
            "inventory": dict(self.inventory),
        }

    def as_dict(self) -> dict:
        return {**self.summary, "findings": [finding.as_dict() for finding in self.findings]}


# --------------------------------------------------------------------------- #
# 清点
# --------------------------------------------------------------------------- #


@dataclass
class Inventory:
    images: list[tuple[str, str]] = field(default_factory=list)  # (路径, 替代文字)
    math: list[str] = field(default_factory=list)
    code: list[str] = field(default_factory=list)
    block_order: list[str] = field(default_factory=list)  # 图片、独立公式、代码块的先后顺序
    tables: list[tuple[int, int, list[list[str]]]] = field(default_factory=list)  # (行, 列, 每格数字)
    notes: int = 0
    superscripts: list[str] = field(default_factory=list)  # 尾注上标编号
    links: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)  # 各行内容器的纯文本（用于编号引用）
    captions: list[str] = field(default_factory=list)  # 图/表的图注纯文本


_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _cell_numbers(blocks: list[dict]) -> list[str]:
    text = " ".join(inline_codec.plain_text(inlines) for inlines in _inline_lists(blocks))
    numbers = [n.replace(",", "") for n in _NUMBER.findall(text)]
    numbers += [str(value) for value in _chinese_values(text)]
    return sorted(numbers)


def _table_shape(content) -> tuple[int, int, list[list[str]]]:
    _, _, colspecs, head, bodies, foot = content
    rows = list(head[1]) + [row for body in bodies for row in body[2] + body[3]] + list(foot[1])
    numbers = [[n for cell in row[1] for n in _cell_numbers(cell[4])] for row in rows]
    return len(rows), len(colspecs), numbers


def _inline_lists(blocks: list[dict]) -> Iterable[list[dict]]:
    """所有行内容器（含表格单元、图注、脚注正文），不进入代码块。"""
    for block in blocks:
        kind, content = block["t"], block.get("c")
        if kind in {"Para", "Plain"}:
            yield content
        elif kind == "Header":
            yield content[2]
        elif kind == "LineBlock":
            yield from content
        elif kind == "BlockQuote":
            yield from _inline_lists(content)
        elif kind == "Div":
            yield from _inline_lists(content[1])
        elif kind == "BulletList":
            for item in content:
                yield from _inline_lists(item)
        elif kind == "OrderedList":
            for item in content[1]:
                yield from _inline_lists(item)
        elif kind == "DefinitionList":
            for term, definitions in content:
                yield term
                for definition in definitions:
                    yield from _inline_lists(definition)
        elif kind == "Figure":
            yield from _inline_lists(_caption(content[1]))
            yield from _inline_lists(content[2])
        elif kind == "Table":
            _, caption, _, head, bodies, foot = content
            yield from _inline_lists(_caption(caption))
            rows = list(head[1]) + [row for body in bodies for row in body[2] + body[3]] + list(foot[1])
            for row in rows:
                for cell in row[1]:
                    yield from _inline_lists(cell[4])


def _caption(caption) -> list[dict]:
    return caption[1] if caption else []


def take_inventory(blocks: list[dict]) -> Inventory:
    found = Inventory()

    def visit(value):
        if isinstance(value, dict):
            kind, content = value.get("t"), value.get("c")
            if kind == "Image":
                found.images.append((content[2][0], inline_codec.plain_text(content[1])))
                found.block_order.append("图片 " + content[2][0])
            elif kind == "Math":
                found.math.append(_normalize(content[1]))
                if content[0]["t"] == "DisplayMath":
                    found.block_order.append("公式 " + _normalize(content[1]))
            elif kind == "Code":
                found.code.append(content[1])
            elif kind == "CodeBlock":
                found.code.append(content[1].rstrip())
                found.block_order.append("代码块 " + content[1].rstrip())
            elif kind == "Note":
                found.notes += 1
            elif kind == "Superscript":
                text = inline_codec.plain_text(content)
                if re.fullmatch(r"\d+", text):
                    found.superscripts.append(text)
            elif kind == "Link" and not content[2][0].startswith("#endnote-"):  # 尾注链接是导出时补上的
                found.links.append(content[2][0])
            elif kind == "Table":
                found.tables.append(_table_shape(content))
                found.captions.append(_blocks_text(_caption(content[1])))
            elif kind == "Figure":
                found.captions.append(_blocks_text(_caption(content[1])))
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            found.superscripts.extend(_raw_superscripts(value))
            for item in value:
                visit(item)

    visit(blocks)
    found.texts = [inline_codec.plain_text(inlines) for inlines in _inline_lists(blocks)]
    return found


def _raw_superscripts(nodes: list) -> list[str]:
    """Markdown 里以原始 HTML 写的尾注上标：<sup>12</sup>。"""
    found = []
    for index in range(len(nodes) - 2):
        opening, middle, closing = nodes[index : index + 3]
        if (
            isinstance(opening, dict)
            and opening.get("t") == "RawInline"
            and opening["c"][1].lower().startswith("<sup")
            and middle.get("t") == "Str"
            and re.fullmatch(r"\d+", middle["c"])
            and closing.get("t") == "RawInline"
            and closing["c"][1].lower() == "</sup>"
        ):
            found.append(middle["c"])
    return found


def _blocks_text(blocks: list[dict]) -> str:
    return " ".join(inline_codec.plain_text(inlines) for inlines in _inline_lists(blocks)).strip()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- #
# 编号引用
# --------------------------------------------------------------------------- #

_NUM = r"\d+(?:[.\-]\d+)*"  # 3、2.1、3-1（"Figure 3-1" 这类按章编号）
_SEQ_EN = rf"\(?{_NUM}\)?(?:\s*(?:,|and|&|or|to|–|—|-)\s*\(?{_NUM}\)?)*"
_SEQ_ZH = rf"[（(]?\s*{_NUM}\s*[)）]?(?:\s*(?:、|，|,|和|与|及|或|至|到|–|—|-|~)\s*[（(]?\s*{_NUM}\s*[)）]?)*"

_EN_PATTERNS = {
    "figure": re.compile(rf"\b(?:Figures?|Figs?\.?)\s*({_SEQ_EN})", re.I),
    "table": re.compile(rf"\bTables?\s+({_SEQ_EN})", re.I),
    "equation": re.compile(rf"\b(?:Equations?|Eqs?\.|Eqns?\.)\s*({_SEQ_EN})", re.I),
    "section": re.compile(rf"(?:\bSections?\s+|§\s*)({_SEQ_EN})", re.I),
    "chapter": re.compile(r"\bChapters?\s+(\d+|[IVXLC]+\b)", re.I),
}
_ZH_LABELS = {
    "figure": r"(?<![地意试企构蓝草版拼制绘])(?P<label>插图|图表|图示|图)",
    "table": r"(?<![列代外发图])(?P<label>表格|表)",
    "equation": r"(?P<label>公式|方程|等式|(?<![形方模格样仪正程算公等])式)",
}
_ZH_PATTERNS = {kind: re.compile(rf"{label}\s*(?P<nums>{_SEQ_ZH})") for kind, label in _ZH_LABELS.items()}
_ZH_SECTION = re.compile(
    rf"(?:第\s*(?P<a>{_NUM}|[一二三四五六七八九十百两]+)\s*(?:小节|节)|(?P<b>{_NUM})\s*(?:小节|节)|§\s*(?P<c>{_SEQ_ZH}))"
)
_ZH_CHAPTER = re.compile(r"第\s*(\d+|[一二三四五六七八九十百两]+)\s*章")

_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
_ZH_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _roman(text: str) -> str:
    values = [_ROMAN[char] for char in text.upper()]
    total = sum(-v if i + 1 < len(values) and v < values[i + 1] else v for i, v in enumerate(values))
    return str(total)


def _zh_int(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    total, number = 0, 0
    for char in text:
        if char in _ZH_DIGITS:
            number = _ZH_DIGITS[char]
        elif char == "十":
            total += (number or 1) * 10
            number = 0
        elif char == "百":
            total += (number or 1) * 100
            number = 0
        else:
            return None
    return total + number


def _chinese_values(text: str) -> list[int]:
    values = []
    for match in re.finditer(r"[一二三四五六七八九十百两]+", text):
        value = _zh_int(match.group(0))
        if value is not None and value > 0:
            values.append(value)
    return values


def _numbers(sequence: str) -> list[str]:
    return re.findall(_NUM, sequence)


def source_references(text: str) -> list[tuple[str, str]]:
    """英文原文中的编号引用：[(类别, 编号)]。"""
    refs = []
    for kind, pattern in _EN_PATTERNS.items():
        for match in pattern.finditer(text):
            if kind == "chapter":
                value = match.group(1)
                refs.append((kind, value if value.isdigit() else _roman(value)))
            else:
                refs += [(kind, number) for number in _numbers(match.group(1))]
    return refs


def target_references(text: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """中文译文中的编号引用与所用标签：([(类别, 编号)], [(类别, 标签写法)])。"""
    refs, labels = [], []
    for kind, pattern in _ZH_PATTERNS.items():
        for match in pattern.finditer(text):
            refs += [(kind, number) for number in _numbers(match.group("nums"))]
            labels.append((kind, match.group("label")))
    for match in _ZH_SECTION.finditer(text):
        if match.group("c"):
            refs += [("section", number) for number in _numbers(match.group("c"))]
        else:
            value = match.group("a") or match.group("b")
            refs.append(("section", value if "." in value else str(_zh_int(value))))
    for match in _ZH_CHAPTER.finditer(text):
        refs.append(("chapter", str(_zh_int(match.group(1)))))
    # 英文标签在译文里原样保留（"Figure 3"）也算同一个引用
    refs += source_references(text)
    return refs, labels


_DEF_EN = {
    "figure": re.compile(rf"^\s*(?:Figure|Fig\.?)\s*({_NUM})\b", re.I),
    "table": re.compile(rf"^\s*Table\s+({_NUM})\b", re.I),
}
_EQ_TAG = re.compile(rf"\\tag\{{\s*({_NUM})\s*\}}|\(\s*({_NUM})\s*\)\s*$")
_HEADER_NUMBER = re.compile(rf"^\s*(?:(?:Section|§)\s*)?({_NUM})\b", re.I)
_HEADER_CHAPTER = re.compile(r"^\s*Chapter\s+(\d+|[IVXLC]+)\b", re.I)


def defined_objects(blocks: list[dict]) -> dict[str, set[str]]:
    """原文中实际存在、带编号的对象：图注/表注开头的编号、公式 \\tag、带编号的标题。"""
    defined: dict[str, set[str]] = {kind: set() for kind in _EN_PATTERNS}
    inventory = take_inventory(blocks)
    for caption in inventory.captions:
        for kind, pattern in _DEF_EN.items():
            if match := pattern.match(caption):
                defined[kind].add(match.group(1))
    for text in inventory.texts:  # 独立的图注段落："Figure 3: ..."
        for kind, pattern in _DEF_EN.items():
            if match := pattern.match(text):
                defined[kind].add(match.group(1))
    for _src, alt in inventory.images:
        if match := _DEF_EN["figure"].match(alt):
            defined["figure"].add(match.group(1))
    for formula in inventory.math:
        if match := _EQ_TAG.search(formula):
            defined["equation"].add(match.group(1) or match.group(2))

    def visit(value):
        if isinstance(value, dict):
            if value.get("t") == "Header":
                title = inline_codec.plain_text(value["c"][2])
                if match := _HEADER_CHAPTER.match(title):
                    number = match.group(1)
                    defined["chapter"].add(number if number.isdigit() else _roman(number))
                elif match := _HEADER_NUMBER.match(title):
                    defined["section"].add(match.group(1))
                    if "." not in match.group(1):
                        defined["chapter"].add(match.group(1))
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(blocks)
    for text in inventory.texts:  # 公式编号写在公式后的文字里："$$...$$ (2)"
        if match := re.fullmatch(rf"\(\s*({_NUM})\s*\)", text.strip()):
            defined["equation"].add(match.group(1))
    return defined


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #

KIND_LABELS = {"figure": "图", "table": "表", "equation": "式", "section": "节", "chapter": "章"}
# 文件名提示（Marker 给图表起名 _page_3_Figure_1，照片是 _Picture_；"page" 本身不说明什么）
_TEXT_IMAGE_HINT = re.compile(r"(?:fig|figure|chart|diagram|table|graph|map|plot|text)", re.I)
_ENGLISH_WORD = re.compile(r"[A-Za-z]{3,}")
_CJK = re.compile(r"[\u3400-\u9fff]")


def _diff(source: list, target: list) -> tuple[list, list]:
    missing = list((Counter(source) - Counter(target)).elements())
    extra = list((Counter(target) - Counter(source)).elements())
    return missing, extra


def _short(text: str, limit: int = 60) -> str:
    text = _normalize(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _compare_sequence(kind: str, label: str, source: list[str], target: list[str], seg_id, findings) -> bool:
    """按多重集合比对内容（行内元素随中文语序调整位置是正常的）；有差异时记一条并返回 True。"""
    missing, extra = _diff(source, target)
    if not (missing or extra):
        return False
    parts = []
    if missing:
        parts.append("译文缺少 " + "、".join(_short(item) for item in missing[:3]))
    if extra:
        parts.append("译文多出或改为 " + "、".join(_short(item) for item in extra[:3]))
    findings.append(AuditFinding(kind, "high", "translation", f"{label}不一致：" + "；".join(parts), seg_id))
    return True


def _audit_unit(unit: AuditUnit, defined: dict[str, set[str]], findings: list[AuditFinding], labels: Counter):
    source = take_inventory(unit.source)
    source_refs = [ref for text in source.texts for ref in source_references(text)]
    for kind, number in source_refs:
        # 子图编号（3-2）只要主图（3）存在就算有：主图图注里常一并列出子图
        if defined.get(kind) and number not in defined[kind] and number.split("-")[0] not in defined[kind]:
            findings.append(
                AuditFinding(
                    "reference",
                    "low",
                    "source",
                    f"原文引用了不存在的{KIND_LABELS[kind]} {number}（书中有编号的{KIND_LABELS[kind]}："
                    f"{', '.join(sorted(defined[kind], key=_sort_key)[:8])}）",
                    unit.seg_id,
                )
            )
    if not unit.translated:
        return
    target = take_inventory(unit.target)
    seg = unit.seg_id
    changed = _compare_sequence(
        "image", "图片", [src for src, _ in source.images], [src for src, _ in target.images], seg, findings
    )
    changed |= _compare_sequence("math", "公式", source.math, target.math, seg, findings)
    changed |= _compare_sequence("code", "代码", source.code, target.code, seg, findings)
    if not changed and source.block_order != target.block_order:
        first = next(a for a, b in zip(source.block_order, target.block_order, strict=True) if a != b)
        findings.append(
            AuditFinding("order", "medium", "translation", f"图片/公式/代码块顺序与原文不同：{_short(first)}", seg)
        )
    _compare_tables(source.tables, target.tables, seg, findings)
    if source.notes != target.notes:
        findings.append(
            AuditFinding(
                "footnote", "high", "translation", f"脚注数量不一致：原文 {source.notes}，译文 {target.notes}", seg
            )
        )
    _compare_sequence("footnote", "尾注编号", source.superscripts, target.superscripts, seg, findings)
    _compare_sequence("link", "链接地址", source.links, target.links, seg, findings)

    target_refs: list[tuple[str, str]] = []
    for text in target.texts:
        refs, used = target_references(text)
        target_refs += refs
        labels.update(used)
    missing, extra = _diff(source_refs, target_refs)
    if missing:
        detail = "、".join(f"{KIND_LABELS[kind]} {number}" for kind, number in missing[:4])
        changed = "、".join(f"{KIND_LABELS[kind]} {number}" for kind, number in extra[:4])
        message = f"编号引用对不上：原文的 {detail} 在译文中找不到" + (f"（译文出现 {changed}）" if changed else "")
        findings.append(AuditFinding("reference", "high", "translation", message, seg))

    for source_caption, target_caption in zip(source.captions, target.captions, strict=False):
        if len(_ENGLISH_WORD.findall(source_caption)) >= 2 and not _CJK.search(target_caption):
            findings.append(
                AuditFinding("caption", "medium", "translation", f"图注未翻译：{_short(target_caption)}", seg)
            )


def _compare_tables(source, target, seg_id, findings) -> None:
    if len(source) != len(target):
        findings.append(
            AuditFinding(
                "table", "high", "translation", f"表格数量不一致：原文 {len(source)}，译文 {len(target)}", seg_id
            )
        )
        return
    for index, ((rows, cols, numbers), (t_rows, t_cols, t_numbers)) in enumerate(
        zip(source, target, strict=True), start=1
    ):
        if (rows, cols) != (t_rows, t_cols):
            findings.append(
                AuditFinding(
                    "table",
                    "high",
                    "translation",
                    f"第 {index} 个表格行列数不一致：原文 {rows}×{cols}，译文 {t_rows}×{t_cols}",
                    seg_id,
                )
            )
            continue
        for row, (expected, actual) in enumerate(zip(numbers, t_numbers, strict=True), start=1):
            if sorted(expected) != sorted(actual):
                missing, extra = _diff(expected, actual)
                findings.append(
                    AuditFinding(
                        "table",
                        "high",
                        "translation",
                        f"第 {index} 个表格第 {row} 行数字改变：原文 {', '.join(missing) or '—'}，"
                        f"译文 {', '.join(extra) or '—'}",
                        seg_id,
                    )
                )


def _sort_key(number: str) -> tuple:
    return tuple(int(part) for part in re.split(r"[.\-]", number))


def _image_paths(blocks: list[dict]) -> list[str]:
    return [src for src, _ in take_inventory(blocks).images]


def _caption_like(block: dict, pattern: re.Pattern) -> str | None:
    if block["t"] not in {"Para", "Plain"}:
        return None
    match = pattern.match(inline_codec.plain_text(block["c"]))
    return match.group(1) if match else None


def _has_image(block: dict) -> bool:
    return block["t"] == "Figure" or bool(_image_paths([block]))


_ZH_FIGURE_CAPTION = re.compile(rf"^\s*(?:插图|图表|图示|图)\s*({_NUM})")


def _adjacent_captions(blocks: list[dict], pattern: re.Pattern) -> set[str]:
    """紧挨着图片的图注段落编号（在每个块列表内看前后兄弟块）。"""
    found: set[str] = set()

    def visit(items: list[dict]):
        for index, block in enumerate(items):
            number = _caption_like(block, pattern)
            if number is not None:
                neighbours = items[max(index - 1, 0) : index] + items[index + 1 : index + 2]
                if any(_has_image(neighbour) for neighbour in neighbours):
                    found.add(number)
            kind, content = block["t"], block.get("c")
            if kind == "BlockQuote":
                visit(content)
            elif kind == "Div":
                visit(content[1])
            elif kind in {"BulletList"}:
                for item in content:
                    visit(item)
            elif kind == "OrderedList":
                for item in content[1]:
                    visit(item)

    visit(blocks)
    return found


def _captioned_images(blocks: list[dict]) -> set[str]:
    """带英文编号图注的图片（"Fig. 3 ..." 紧邻或在 Figure 图注里）：多为图表，图内常有英文标注。"""
    found: set[str] = set()

    def visit(items: list[dict]):
        for index, block in enumerate(items):
            if block["t"] == "Figure" and _DEF_EN["figure"].match(_blocks_text(_caption(block["c"][1]))):
                found.update(_image_paths([block]))
            elif _caption_like(block, _DEF_EN["figure"]) is not None:
                for neighbour in items[max(index - 1, 0) : index] + items[index + 1 : index + 2]:
                    found.update(_image_paths([neighbour]))
            kind, content = block["t"], block.get("c")
            if kind == "BlockQuote":
                visit(content)
            elif kind == "Div":
                visit(content[1])

    visit(blocks)
    return found


def _is_local(src: str) -> bool:
    return bool(src) and not src.startswith(("http://", "https://", "data:", "//"))


def audit_document(
    units: list[AuditUnit],
    source_blocks: list[dict],
    target_blocks: list[dict],
    image_exists: Callable[[str], bool] | None = None,
    source_dead_links: Iterable[str] = (),
    target_dead_links: Iterable[str] = (),
) -> AuditReport:
    """逐组比对 + 全书级检查（对象是否存在、标签译法统一、图注相邻、图片文件、死链）。"""
    findings: list[AuditFinding] = []
    defined = defined_objects(source_blocks)
    labels: Counter = Counter()
    for unit in units:
        _audit_unit(unit, defined, findings, labels)

    by_kind: dict[str, Counter] = {}
    for (kind, label), count in labels.items():
        by_kind.setdefault(kind, Counter())[label] = count
    for kind, counter in by_kind.items():
        if len(counter) > 1:
            usage = "、".join(f"「{label}」{count} 处" for label, count in counter.most_common())
            findings.append(
                AuditFinding("label", "medium", "translation", f"{KIND_LABELS[kind]}的标签译法不统一：{usage}")
            )

    source_adjacent = _adjacent_captions(source_blocks, _DEF_EN["figure"])
    target_adjacent = _adjacent_captions(target_blocks, _ZH_FIGURE_CAPTION) | _adjacent_captions(
        target_blocks, _DEF_EN["figure"]
    )
    for number in sorted(source_adjacent - target_adjacent, key=_sort_key):
        findings.append(AuditFinding("caption", "medium", "translation", f"图 {number} 的图注不再与图相邻"))

    source_inventory = take_inventory(source_blocks)
    if image_exists is not None:
        for src in dict.fromkeys(src for src, _ in source_inventory.images):
            if _is_local(src) and not image_exists(src):
                findings.append(AuditFinding("image", "high", "source", f"图片文件不存在：{src}"))
        for src in dict.fromkeys(_image_paths(target_blocks)):
            if _is_local(src) and src not in {s for s, _ in source_inventory.images} and not image_exists(src):
                findings.append(AuditFinding("image", "high", "translation", f"译文引用的图片文件不存在：{src}"))

    source_dead = set(source_dead_links)
    for target in sorted(source_dead):
        findings.append(AuditFinding("link", "low", "source", f"原文内部链接指向不存在的锚点：#{target}"))
    for target in sorted(set(target_dead_links) - source_dead):
        findings.append(AuditFinding("link", "high", "translation", f"译文内部链接指向不存在的锚点：#{target}"))

    captioned = _captioned_images(source_blocks)
    for src, alt in dict.fromkeys(source_inventory.images):
        name = src.rsplit("/", 1)[-1]
        if src in captioned or _ENGLISH_WORD.search(alt) or _TEXT_IMAGE_HINT.search(name):
            findings.append(
                AuditFinding("image_text", "info", "notice", f"图片可能含英文文字（不翻译图内文字，需人工确认）：{src}")
            )

    inventory = {
        "images": len(source_inventory.images),
        "formulas": len(source_inventory.math),
        "code": len(source_inventory.code),
        "tables": len(source_inventory.tables),
        "footnotes": source_inventory.notes,
        "links": len(source_inventory.links),
        **{f"numbered_{kind}s": len(numbers) for kind, numbers in defined.items()},
    }
    return AuditReport(findings, inventory)
