"""
行内标签编解码（Inline Tag Codec）

把 pandoc AST 的行内节点列表编码成"纯文字 + 内联标签"，交给 LLM 翻译；
再把带标签的译文解码回行内节点。结构信息（公式、链接地址、图片、脚注、代码）
始终由程序持有，LLM 只看到占位标签，物理上无法改写它们。

    The <a1>CCRU</a1> defined <b2>hyperstition</b2> as <m3/><fn4/>.

- 成对标签 `<b1>…</b1>`：包裹可翻译的格式化文字（加粗、斜体、链接文字……），
  允许在句中随语序移动；
- 原子标签 `<m1/>`：不可翻译、必须原样保留的节点（公式、代码、图片、脚注……）。

解码时以"当前"源行内列表重新编码得到标签 → 节点映射，因此可以对源 AST 的
深拷贝做替换（脚注正文先被替换为译文，再随段落一起还原）。
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from dataclasses import dataclass, field

# 成对标签：pandoc 节点类型 → 标签名
PAIR_TAGS = {
    "Strong": "b",
    "Emph": "i",
    "Underline": "u",
    "Strikeout": "s",
    "Superscript": "sup",
    "Subscript": "sub",
    "SmallCaps": "sc",
    "Quoted": "q",
    "Link": "a",
    "Span": "span",
}

# 原子标签：pandoc 节点类型 → 标签名（内容不送 LLM）
ATOMIC_TAGS = {
    "Math": "m",
    "Code": "code",
    "Image": "img",
    "Note": "fn",
    "RawInline": "raw",
    "Cite": "cite",
    "LineBreak": "br",
}

_TAG_RE = re.compile(r"<(/?)([a-z]+)(\d+)(/?)>")


@dataclass
class EncodedInlines:
    """编码结果：带标签文本 + 标签表（标签名 → 源节点）。"""

    text: str
    tags: dict[str, dict] = field(default_factory=dict)
    notes: list[dict] = field(default_factory=list)


@dataclass
class DecodeResult:
    inlines: list[dict]
    problems: list[str] = field(default_factory=list)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _unescape(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _content_index(node_type: str) -> int | None:
    """成对节点中"子行内列表"所在的 c 下标。"""
    if node_type in {"Strong", "Emph", "Underline", "Strikeout", "Superscript", "Subscript", "SmallCaps"}:
        return None  # c 本身就是行内列表
    if node_type in {"Quoted", "Span"}:
        return 1
    if node_type == "Link":
        return 1
    raise ValueError(node_type)


def pair_children(node: dict) -> list[dict]:
    index = _content_index(node["t"])
    return node["c"] if index is None else node["c"][index]


def _with_children(node: dict, children: list[dict]) -> dict:
    new = {"t": node["t"], "c": copy.deepcopy(node["c"])}
    index = _content_index(node["t"])
    if index is None:
        new["c"] = children
    else:
        new["c"][index] = children
    return new


def _is_anchor_span(node: dict) -> bool:
    """空内容且带 id 的 Span：EPUB 中的锚点（目录跳转目标），整体保留。"""
    return node["t"] == "Span" and not node["c"][1]


def encode(inlines: list[dict]) -> EncodedInlines:
    """把行内节点列表编码为带标签文本。"""
    result = EncodedInlines(text="")
    counter = 0
    parts: list[str] = []

    def walk(nodes: list[dict]):
        nonlocal counter
        for node in nodes:
            kind = node["t"]
            if kind == "Str":
                parts.append(_escape(node["c"]))
            elif kind in {"Space", "SoftBreak"}:
                parts.append(" ")
            elif kind in ATOMIC_TAGS or _is_anchor_span(node):
                counter += 1
                name = f"{'anchor' if kind == 'Span' else ATOMIC_TAGS[kind]}{counter}"
                result.tags[name] = node
                parts.append(f"<{name}/>")
                if kind == "Note":
                    result.notes.append(node)
            elif kind in PAIR_TAGS:
                counter += 1
                name = f"{PAIR_TAGS[kind]}{counter}"
                result.tags[name] = node
                parts.append(f"<{name}>")
                walk(pair_children(node))
                parts.append(f"</{name}>")
            else:  # 未知节点：按原子处理，保证不丢失
                counter += 1
                name = f"x{counter}"
                result.tags[name] = node
                parts.append(f"<{name}/>")

    walk(inlines)
    result.text = re.sub(r" {2,}", " ", "".join(parts)).strip()
    return result


def _append_text(nodes: list[dict], text: str) -> None:
    """把纯文本追加为 Str/Space 节点（就地写入 nodes，以便判断与前文的空格衔接）。"""
    for piece in re.split(r"(\s+)", _unescape(text)):
        if not piece:
            continue
        if piece.isspace():
            if nodes and nodes[-1]["t"] != "Space":
                nodes.append({"t": "Space"})
        else:
            nodes.append({"t": "Str", "c": piece})


def decode(tagged: str, source_inlines: list[dict]) -> DecodeResult:
    """把带标签译文解码回行内节点。

    source_inlines 用于重建标签 → 节点映射；容错处理：
    未知标签丢弃、多余闭合标签忽略、未闭合标签在末尾补齐，均记入 problems。
    """
    tags = encode(source_inlines).tags
    problems: list[str] = []
    root: list[dict] = []
    stack: list[tuple[str, list[dict]]] = [("", root)]
    used: set[str] = set()

    position = 0
    for match in _TAG_RE.finditer(tagged):
        _append_text(stack[-1][1], tagged[position : match.start()])
        position = match.end()
        closing, base, number, self_closing = match.groups()
        name = f"{base}{number}"
        node = tags.get(name)
        if node is None:
            problems.append(f"未知标签 <{name}>")
            continue
        if self_closing:
            if node["t"] in PAIR_TAGS and not _is_anchor_span(node):
                # 成对标签被写成自闭合：保留空格式节点，不丢链接地址
                stack[-1][1].append(_with_children(node, []))
            else:
                stack[-1][1].append(copy.deepcopy(node))
            used.add(name)
        elif not closing:
            if node["t"] not in PAIR_TAGS:
                stack[-1][1].append(copy.deepcopy(node))
                used.add(name)
                continue
            stack.append((name, []))
        else:
            if not any(open_name == name for open_name, _ in stack[1:]):
                problems.append(f"多余的闭合标签 </{name}>")
                continue
            while stack[-1][0] != name:
                dangling, children = stack.pop()
                problems.append(f"标签 <{dangling}> 未闭合")
                stack[-1][1].append(_with_children(tags[dangling], children))
            _, children = stack.pop()
            stack[-1][1].append(_with_children(node, children))
            used.add(name)
    _append_text(stack[-1][1], tagged[position:])
    while len(stack) > 1:
        dangling, children = stack.pop()
        problems.append(f"标签 <{dangling}> 未闭合")
        stack[-1][1].append(_with_children(tags[dangling], children))
        used.add(dangling)

    _trim_spaces(root)
    return DecodeResult(inlines=root, problems=problems)


def _trim_spaces(nodes: list[dict]) -> None:
    while nodes and nodes[0]["t"] == "Space":
        nodes.pop(0)
    while nodes and nodes[-1]["t"] == "Space":
        nodes.pop()


def plain_text(inlines: list[dict]) -> str:
    """行内节点的可读纯文本（公式/代码/图片/脚注不计入）。"""
    parts: list[str] = []

    def walk(nodes):
        for node in nodes:
            kind = node["t"]
            if kind == "Str":
                parts.append(node["c"])
            elif kind in {"Space", "SoftBreak", "LineBreak"}:
                parts.append(" ")
            elif kind in PAIR_TAGS and not _is_anchor_span(node):
                walk(pair_children(node))

    walk(inlines)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def skeleton(inlines: list[dict]) -> tuple[Counter, Counter]:
    """结构骨架：(必须保真的原子/链接签名, 格式化标记计数)。

    第一项必须完全一致（公式、代码、图片、链接地址、脚注、锚点、原始内容）；
    第二项（加粗/斜体等）允许因中文表达习惯而增减，仅作低严重度提示。
    """
    strict: Counter = Counter()
    formatting: Counter = Counter()

    def walk(nodes):
        for node in nodes:
            kind = node["t"]
            if kind == "Math":
                strict[("公式", node["c"][1].strip())] += 1
            elif kind == "Code":
                strict[("代码", node["c"][1])] += 1
            elif kind == "Image":
                strict[("图片", node["c"][2][0])] += 1
            elif kind == "Note":
                strict[("脚注", "")] += 1
            elif kind == "RawInline":
                strict[("原始内容", node["c"][1])] += 1
            elif kind == "Cite":
                strict[("引用", ",".join(c["citationId"] for c in node["c"][0]))] += 1
            elif kind == "Link":
                strict[("链接", node["c"][2][0])] += 1
                walk(node["c"][1])
            elif kind == "Span":
                if node["c"][0][0]:
                    strict[("锚点", node["c"][0][0])] += 1
                walk(node["c"][1])
            elif kind in PAIR_TAGS:
                formatting[PAIR_TAGS[kind]] += 1
                walk(pair_children(node))

    walk(inlines)
    return strict, formatting


def compare_skeletons(source: list[dict], translation: list[dict]) -> tuple[list[str], list[str]]:
    """比较骨架，返回 (严重问题, 轻微问题) 描述。"""
    src_strict, src_fmt = skeleton(source)
    tgt_strict, tgt_fmt = skeleton(translation)
    severe: list[str] = []
    for (label, value), count in (src_strict - tgt_strict).items():
        severe.append(f"缺少{label}" + (f" `{value[:60]}`" if value else "") + (f" ×{count}" if count > 1 else ""))
    for (label, value), count in (tgt_strict - src_strict).items():
        severe.append(f"多出{label}" + (f" `{value[:60]}`" if value else "") + (f" ×{count}" if count > 1 else ""))
    minor = []
    # 斜体与引号不计：中文习惯把斜体书名写成《》、把引号写成“”，丢掉这两类标签是正确译法
    ignored = {"i", "q", "span"}
    src_fmt = Counter({k: v for k, v in src_fmt.items() if k not in ignored})
    tgt_fmt = Counter({k: v for k, v in tgt_fmt.items() if k not in ignored})
    if src_fmt != tgt_fmt:
        minor.append("行内格式（加粗/斜体等）数量与原文不同")
    return severe, minor
