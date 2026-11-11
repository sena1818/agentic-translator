"""
分类型修复的纯规则：问题分路、接受判定、约束式编辑的校验与应用。

修复按问题类型分四路，一段同时有多类问题时按固定顺序逐路处理，每路之后重新质检：

1. retranslate  缺段、截断、空译、未翻译 → 重译该段（之后的各路都在新译文上做）
2. structure    结构标签错误 → 只修标签、不改文字
3. number       数字不符 → 约束式编辑，只改数字所在的最短片段
4. terminology  术语未按表 → 约束式编辑，只把那一处换成术语表译名

接受标准：目标类型的问题全部消失，且没有新增任何其他类型的问题（只看 high / medium；
low 级只是提示，不触发修复，也不阻止接受）。否则保留原译、记下拒绝原因，待人工处理。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

ROUTE_ORDER = ("retranslate", "structure", "number", "terminology")
ROUTE_KINDS = {
    "retranslate": frozenset({"missing", "truncated", "empty", "untranslated"}),
    "structure": frozenset({"structure"}),
    "number": frozenset({"fidelity"}),
    "terminology": frozenset({"terminology"}),
}
ROUTE_LABELS = {"retranslate": "重译", "structure": "标签", "number": "数字", "terminology": "术语"}
SEVERE = frozenset({"high", "medium"})
# 约束式编辑走的两路：模型只回"把哪段换成什么"，由程序替换，其余文字物理上不会被改
EDIT_ROUTES = frozenset({"number", "terminology"})


class IssueLike(Protocol):
    kind: str
    severity: str
    message: str


def route_of(kind: str) -> str | None:
    for route, kinds in ROUTE_KINDS.items():
        if kind in kinds:
            return route
    return None


def routes_needed(issues: list[IssueLike]) -> list[str]:
    """按固定顺序列出这段需要走的修复路（只看 high / medium 问题）。"""
    wanted = {route_of(issue.kind) for issue in issues if issue.severity in SEVERE}
    return [route for route in ROUTE_ORDER if route in wanted]


def _severe_signatures(issues: list[IssueLike]) -> set[tuple[str, str]]:
    return {(issue.kind, issue.message) for issue in issues if issue.severity in SEVERE}


def accept_repair(route: str, before: list[IssueLike], after: list[IssueLike]) -> tuple[bool, str]:
    """目标问题消失且没有新增其他类型的问题才接受；返回 (是否接受, 拒绝原因)。"""
    target = ROUTE_KINDS[route]
    remaining = [issue.message for issue in after if issue.kind in target and issue.severity in SEVERE]
    if remaining:
        return False, "目标问题仍在：" + "；".join(remaining[:2])
    new = sorted(
        message for kind, message in _severe_signatures(after) - _severe_signatures(before) if kind not in target
    )
    if new:
        return False, "新增问题：" + "；".join(new[:2])
    return True, ""


def no_regression(before: list[IssueLike], after: list[IssueLike]) -> bool:
    """整块修复（非结构化模式）的接受标准：严重问题变少，且没有出现原来没有的严重问题。"""
    old, new = _severe_signatures(before), _severe_signatures(after)
    return not (new - old) and len(new) < len(old)


# --------------------------------------------------------------------------- #
# 约束式编辑
# --------------------------------------------------------------------------- #

_EDIT_RE = re.compile(r'<edit\s+id="?(\d+)"?\s*>\s*<from>(.*?)</from>\s*<to>(.*?)</to>\s*</edit>', re.S)
_NUMERIC = re.compile(r"[0-9０-９零〇一二两三四五六七八九十百千万亿]")
_MAX_TERM_SPAN = 12  # 被换掉的错误译名最多这么长；再长就不是"只换术语"了
_MAX_NUMBER_SPAN = 12


@dataclass(frozen=True)
class Edit:
    old: str
    new: str


def parse_edits(raw: str) -> dict[int, list[Edit]]:
    edits: dict[int, list[Edit]] = {}
    for local_id, old, new in _EDIT_RE.findall(raw):
        edits.setdefault(int(local_id), []).append(Edit(old.strip(), new.strip()))
    return edits


def _diff_middle(old: str, new: str) -> tuple[str, str, str, str]:
    """去掉公共前后缀，返回 (前缀, 旧中段, 新中段, 后缀)。"""
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(old), len(new)) - prefix and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]:
        suffix += 1
    return old[:prefix], old[prefix : len(old) - suffix], new[prefix : len(new) - suffix], old[len(old) - suffix :]


def check_edit(route: str, edit: Edit, text: str, renderings: list[str]) -> str:
    """校验一条编辑；合法返回空串，否则返回原因。

    - from 必须在当前译文里恰好出现一次，from / to 都不能碰标签；
    - 术语：to 必须由 from 的前缀 + 术语表译名 + from 的后缀组成，被换掉的部分不超过 _MAX_TERM_SPAN 字；
    - 数字：只允许改动不超过 _MAX_NUMBER_SPAN 字的片段，且新片段里有数字。
    """
    if not edit.old:
        return "from 为空"
    if text.count(edit.old) != 1:
        return "from 不在当前译文中" if edit.old not in text else "from 在译文中出现多次"
    if "<" in edit.old + edit.new or ">" in edit.old + edit.new:
        return "编辑涉及标签"
    if route == "terminology":
        for rendering in sorted(renderings, key=len, reverse=True):
            index = edit.new.find(rendering)
            while index >= 0:
                head, tail = edit.new[:index], edit.new[index + len(rendering) :]
                if (
                    edit.old.startswith(head)
                    and edit.old.endswith(tail)
                    and len(head) + len(tail) <= len(edit.old)
                    and len(edit.old) - len(head) - len(tail) <= _MAX_TERM_SPAN
                ):
                    return ""
                index = edit.new.find(rendering, index + 1)
        return "to 不是只把一处换成术语表译名"
    if route == "number":
        _, old_mid, new_mid, _ = _diff_middle(edit.old, edit.new)
        if len(old_mid) > _MAX_NUMBER_SPAN or len(new_mid) > _MAX_NUMBER_SPAN:
            return "改动超出数字所在的片段"
        if not _NUMERIC.search(new_mid):
            return "改动里没有数字"
        return ""
    return f"{route} 不接受编辑"


def apply_edits(route: str, text: str, edits: list[Edit], renderings: list[str]) -> tuple[str, list[str]]:
    """逐条校验并应用编辑，返回 (新译文, 被丢弃的编辑原因)。"""
    problems = []
    for edit in edits:
        reason = check_edit(route, edit, text, renderings)
        if reason:
            problems.append(reason)
            continue
        text = text.replace(edit.old, edit.new, 1)
    return text, problems


def text_unchanged(before_plain: str, after_plain: str) -> bool:
    """标签修复不许改文字：去掉空白后逐字相同。"""
    return re.sub(r"\s+", "", before_plain) == re.sub(r"\s+", "", after_plain)
