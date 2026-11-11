"""
交付形式：对照阅读器的图片只内联一份、缺图清单、EPUB（仅译文 / 双语）。

- 离线对照阅读器里同一张图出现在原文栏和译文栏，过去各内联一次 data URI，体积翻倍。
  这里把图片放进页面里的一份 JSON 表，两栏的 <img> 只带编号，由脚本在加载时填上。
- 找不到的本地图片收集成清单写进导出统计，不再静默留下坏链接。
- EPUB 由 pandoc 从同一份译后 AST 生成：按一级标题分章、带目录、脚注回链；图片解析为本地文件打进包里，
  找不到或位于网络上的图片换成"[图片缺失：…]"文字，避免生成时联网或留下坏引用。
"""

from __future__ import annotations

import base64
import copy
import html
import logging
import mimetypes
import re
from collections.abc import Callable
from pathlib import Path

from ...infrastructure.document import pandoc

logger = logging.getLogger(__name__)

ImageLocator = Callable[[str], Path | None]

_IMG_RE = re.compile(r"<img\b[^>]*>")
_SRC_RE = re.compile(r'\bsrc="([^"]*)"')
_REMOTE = ("http://", "https://", "data:")


def data_uri(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def resolver_from_locator(locator: ImageLocator) -> Callable[[str], str]:
    """本地图片 → data URI；找不到时原样返回（由 ImageInliner 记为缺图）。"""

    def resolve(src: str) -> str:
        if src.startswith(_REMOTE):
            return src
        path = locator(src)
        return data_uri(path) if path is not None else src

    return resolve


class ImageInliner:
    """改写 HTML 片段里的 <img>：内联图片按内容去重编号，同一张图在多栏里只存一份。"""

    def __init__(self, resolver: Callable[[str], str]):
        self.resolver = resolver
        self.images: dict[str, str] = {}  # 编号 → data URI
        self.missing: list[str] = []
        self._keys: dict[str, str] = {}  # data URI → 编号

    def rewrite(self, fragment: str) -> str:
        return _IMG_RE.sub(self._rewrite_tag, fragment)

    def _rewrite_tag(self, match: re.Match[str]) -> str:
        tag = match.group(0)
        found = _SRC_RE.search(tag)
        if not found:
            return tag
        src = html.unescape(found.group(1))
        resolved = self.resolver(src)
        if resolved.startswith("data:"):
            key = self._keys.get(resolved)
            if key is None:
                key = self._keys[resolved] = f"i{len(self._keys)}"
                self.images[key] = resolved
            return tag[: found.start()] + f'data-img="{key}"' + tag[found.end() :]
        if resolved == src and not src.startswith(_REMOTE):
            if src not in self.missing:
                self.missing.append(src)
            return tag
        return tag[: found.start()] + f'src="{html.escape(resolved, quote=True)}"' + tag[found.end() :]


# ---------------------------------------------------------------------- #
# EPUB
# ---------------------------------------------------------------------- #


def _alt_text(inlines: list[dict]) -> str:
    parts = []
    for node in inlines:
        if node["t"] == "Str":
            parts.append(node["c"])
        elif node["t"] in {"Space", "SoftBreak"}:
            parts.append(" ")
    return "".join(parts).strip()


def localize_images(value, locator: ImageLocator | None, missing: list[str]) -> None:
    """把 AST 里的图片目标改成本地绝对路径；找不到或在网络上的图片换成说明文字。"""
    if isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, dict) and item.get("t") == "Image":
                attr, alt, (target, image_title) = item["c"]
                if target.startswith("data:"):
                    continue
                path = None if target.startswith(_REMOTE[:2]) or locator is None else locator(target)
                if path is not None:
                    item["c"] = [attr, alt, [str(path.resolve()), image_title]]
                    continue
                if target not in missing:
                    missing.append(target)
                label = _alt_text(alt) or Path(target).name
                value[index] = {"t": "Emph", "c": [{"t": "Str", "c": f"[图片缺失：{label}]"}]}
            else:
                localize_images(item, locator, missing)
    elif isinstance(value, dict):
        for item in value.values():
            localize_images(item, locator, missing)


def write_epub(
    ast_template: dict,
    blocks: list[dict],
    path: Path,
    title: str,
    locator: ImageLocator | None,
    sanitize: Callable[[list], None] | None = None,
) -> list[str]:
    """写出一个 EPUB，返回其中缺失的图片。"""
    blocks = copy.deepcopy(blocks)
    if sanitize is not None:
        sanitize(blocks)
    missing: list[str] = []
    localize_images(blocks, locator, missing)
    pandoc.blocks_to_epub(ast_template, blocks, str(path), title=title)
    return missing
