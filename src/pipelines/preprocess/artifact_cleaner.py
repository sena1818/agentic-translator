"""
EPUB / Pandoc 残留清理器

目标：
- 去除 Kindle / Calibre 的分页残留与空锚点
- 合并被拆开的图片类属性并修复图片路径
- 处理常见的 `[标题]{.calibreX}` 残留
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - 依赖缺失时退化为无规则库模式
    yaml = None

from src.utils.config_loader import get_config


class EpubArtifactCleaner:
    """清理 EPUB 转 Markdown 后常见的结构残留"""

    def __init__(self):
        self.rule_sets = self._load_rules()
        self.rules = self._resolve_rules("epub")

    def clean(self, text: str, source_type: str = "epub") -> str:
        self.rules = self._resolve_rules(source_type)
        text = self.repair_internal_links(text)
        text = self._remove_pagebreak_blocks(text)
        text = self._remove_svg_wrapper_blocks(text)
        text = self._merge_multiline_image_attributes(text)
        text = self._normalize_escaped_list_markers(text)
        text = self._remove_anchor_lines(text)
        text = self._remove_empty_quote_lines(text)
        text = self._normalize_standalone_spans(text)
        text = self._normalize_inline_spans(text)
        text = self._strip_inline_class_suffixes(text)
        text = self._normalize_standalone_bracket_lines(text)
        text = self._remove_symbol_only_lines(text)
        text = self._remove_orphan_class_lines(text)
        text = self._normalize_images(text)
        text = self._collapse_blank_lines(text)
        return text.strip() + "\n"

    def _load_rules(self) -> dict[str, Any]:
        config = get_config()
        rules_path = config.artifact_rules_path
        if yaml is None or not rules_path.exists():
            return {}
        with open(rules_path, encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        return payload

    def _resolve_rules(self, source_type: str) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        self._merge_rule_section(merged, self.rule_sets.get("common", {}))
        self._merge_source_rules(merged, source_type)
        return merged

    def _merge_source_rules(self, merged: dict[str, Any], source_type: str):
        if not source_type:
            return
        section = self.rule_sets.get(source_type, {})
        for parent in section.get("inherit_from", []):
            self._merge_source_rules(merged, parent)
        self._merge_rule_section(merged, section)

    def _merge_rule_section(self, merged: dict[str, Any], section: dict[str, Any]):
        for key, value in section.items():
            if key == "inherit_from":
                continue
            if isinstance(value, list):
                merged.setdefault(key, [])
                merged[key].extend(value)
            elif isinstance(value, dict):
                merged.setdefault(key, {})
                merged[key].update(value)
            else:
                merged[key] = value

    def _remove_pagebreak_blocks(self, text: str) -> str:
        patterns = self.rules.get("strip_block_patterns") or [
            r"(?ms)^(?:>\s*)?:::\s*\{#[^}\n]*\.mbp_pagebreak[^}\n]*\}\s*\n(?:>\s*)?:::\s*\n?",
        ]
        for pattern in patterns:
            text = re.sub(pattern, "", text)
        text = re.sub(
            r"(?m)^(?:>\s*)?:::\s*\{#[^}\n]*\.mbp_pagebreak[^}\n]*\}\s*$",
            "",
            text,
        )
        return text

    def _remove_svg_wrapper_blocks(self, text: str) -> str:
        # 逐行扫描：配对 `::: {}` 与其后最近的 `:::`，块内含 SVG/图片时整块删除
        opener = re.compile(r"^(?:>[ \t]*)*:::[ \t]*\{\}[ \t]*$")
        closer = re.compile(r"^(?:>[ \t]*)*:::[ \t]*$")
        lines = text.splitlines(keepends=True)
        kept: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            if not opener.match(line.rstrip("\r\n")):
                kept.append(line)
                index += 1
                continue
            end = index + 1
            while end < len(lines) and not closer.match(lines[end].rstrip("\r\n")):
                end += 1
            if end == len(lines):
                kept.extend(lines[index:])
                break
            block = "".join(lines[index : end + 1])
            if "<svg" not in block and "<image" not in block:
                kept.append(block)
            index = end + 1
        return "".join(kept)

    def _merge_multiline_image_attributes(self, text: str) -> str:
        return re.sub(
            r"((?:>\s*)?!\[[^\]]*\]\([^)]+\))\s*\n(?:>\s*)?\{[^}\n]+\}",
            r"\1",
            text,
        )

    @staticmethod
    def repair_internal_links(text: str) -> str:
        """修复 EPUB 内部链接的目标。

        pandoc 读 EPUB 时会丢掉段落上的 id（Calibre 常把锚点放在 `<p id="filepos843">` 上），
        目录里的 `#index_split_001.html#filepos843` 因此失效；但每个文件一级的锚点
        `index_split_001.html` 仍在。目标不存在而所在文件存在时，退回指向文件开头（章首），
        至少保证目录能跳到正确的章节。
        """
        ids = set(re.findall(r"\{#([^\s}]+)", text))
        if not ids:
            return text

        def retarget(match: re.Match[str]) -> str:
            target = match.group(1)
            if target not in ids and "#" in target and target.split("#", 1)[0] in ids:
                target = target.split("#", 1)[0]
            return f"](#{target.replace('#', '__')})"

        text = re.sub(r"\]\(#([^)\s]+)\)", retarget, text)
        # pandoc 读 Markdown 时 id 里的 `#` 之后才算 id（`{#ch2.xhtml#sec}` → `sec`），
        # 统一改写为 `__`，既保持链接可解析，也避免不同章节的同名锚点相撞
        return re.sub(r"\{#([^\s}]+)", lambda m: "{#" + m.group(1).replace("#", "__"), text)

    def _remove_anchor_lines(self, text: str) -> str:
        # 被内部链接（目录、交叉引用、脚注回链）指向的锚点是跳转目标，必须保留
        referenced = set(re.findall(r"\]\(#([^)\s]+)\)", text))

        def keep_referenced(match: re.Match[str]) -> str:
            ids = re.findall(r"\{#([^}\s]+)", match.group(0))
            return match.group(0) if any(anchor in referenced for anchor in ids) else ""

        patterns = self.rules.get("strip_line_patterns") or []
        for pattern in patterns:
            text = re.sub(pattern, keep_referenced, text)
        text = re.sub(r"\[\]\{#[^}]+\}", keep_referenced, text)
        return text

    def _normalize_escaped_list_markers(self, text: str) -> str:
        return re.sub(r"(?m)^(\s*(?:>\s*)?\d+)\\([.)])\s+", r"\1\2 ", text)

    def _normalize_standalone_spans(self, text: str) -> str:
        cleaned_lines = []
        for line in text.splitlines():
            quote_prefix, stripped = self._split_quote_prefix(line)
            if "{." not in stripped or "[" not in stripped:
                cleaned_lines.append(line)
                continue
            unwrapped = self._unwrap_line_class_wrapper(stripped)
            if not unwrapped:
                malformed = re.match(r"^\[(.+)\]\{\.([A-Za-z0-9_]+)\]$", stripped)
                if not malformed:
                    cleaned_lines.append(line)
                    continue
                content, class_name = malformed.groups()
            else:
                content, class_name = unwrapped
            content = self._unwrap_span_content(content).replace("\u00a0", " ").strip()
            if not content:
                continue
            if content in set(self.rules.get("strip_symbol_lines", [])):
                continue

            if self._should_promote_to_heading(content, class_name):
                cleaned_lines.append(
                    f"{quote_prefix}{self._heading_prefix(class_name)} {content}"
                )
            else:
                cleaned_lines.append(f"{quote_prefix}{content}")

        return "\n".join(cleaned_lines)

    def _normalize_inline_spans(self, text: str) -> str:
        cleaned_lines = []
        for line in text.splitlines():
            quote_prefix, stripped = self._split_quote_prefix(line)
            if "{." not in stripped or "[" not in stripped:
                cleaned_lines.append(line)
                continue
            normalized = self._unwrap_span_content(stripped)
            if normalized != stripped:
                cleaned_lines.append(f"{quote_prefix}{normalized}")
            else:
                cleaned_lines.append(line)
        return "\n".join(cleaned_lines)

    def _normalize_standalone_bracket_lines(self, text: str) -> str:
        if not self.rules.get("unwrap_bracket_lines", True):
            return text

        max_length = int(self.rules.get("bracket_line_max_length", 80))
        cleaned_lines = []
        for line in text.splitlines():
            quote_prefix, stripped = self._split_quote_prefix(line)
            match = re.match(r"^\[([^\]]+)\]$", stripped)
            if not match:
                cleaned_lines.append(line)
                continue
            content = match.group(1).replace("\u00a0", " ").strip()
            if not content:
                continue
            if len(content) > max_length:
                cleaned_lines.append(line)
                continue
            cleaned_lines.append(f"{quote_prefix}{content}")
        return "\n".join(cleaned_lines)

    def _remove_symbol_only_lines(self, text: str) -> str:
        symbols = set(self.rules.get("strip_symbol_lines", []))
        if not symbols:
            return text

        cleaned_lines = []
        for line in text.splitlines():
            quote_prefix, stripped = self._split_quote_prefix(line)
            if stripped in symbols:
                continue
            cleaned_lines.append(line if not quote_prefix else f"{quote_prefix}{stripped}")
        return "\n".join(cleaned_lines)

    def _remove_empty_quote_lines(self, text: str) -> str:
        if not self.rules.get("drop_empty_quote_lines", False):
            return text
        cleaned_lines = []
        for line in text.splitlines():
            if re.match(r"^(?:\s*>\s*)+$", line):
                continue
            cleaned_lines.append(line)
        return "\n".join(cleaned_lines)

    def _strip_inline_class_suffixes(self, text: str) -> str:
        prefixes = tuple(self.rules.get("strip_inline_classes", []))
        if not prefixes:
            return text

        pattern = re.compile(r"\]\{\.([A-Za-z0-9_]+)\}")

        def replace(match: re.Match[str]) -> str:
            class_name = match.group(1).lower()
            if any(class_name.startswith(prefix.lower()) for prefix in prefixes if prefix):
                return ""
            return match.group(0)

        return pattern.sub(replace, text)

    def _remove_orphan_class_lines(self, text: str) -> str:
        patterns = [
            r"(?m)^(?:>\s*)?\{\.?[A-Za-z0-9_\-]+\}\s*$",
            r"(?m)^(?:>\s*)?\]\{\.?[A-Za-z0-9_\-]+\}\s*$",
            r"(?m)^(?:>\s*)?\[\s*\]\{\.?[A-Za-z0-9_\-]+\}\s*$",
            r"(?m)^(?:>\s*)?\[[^\]]*\]\{\.?[A-Za-z0-9_\-]+\]\s*$",
        ]
        for pattern in patterns:
            text = re.sub(pattern, "", text)
        return text

    def _normalize_images(self, text: str) -> str:
        def replace(match):
            alt = (match.group(1) or "").strip()
            path = match.group(2).strip()
            filename = Path(path).name
            lowered = path.lower()
            relative_path = f"images/{filename}" if "/images/" in lowered else filename
            if not alt or alt.upper() == "PIC":
                alt = Path(filename).stem
            return f"![{alt}]({relative_path})"

        return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", replace, text)

    def _collapse_blank_lines(self, text: str) -> str:
        text = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", text)
        return text

    def _should_promote_to_heading(self, content: str, class_name: str) -> bool:
        if not class_name.startswith("calibre"):
            return False
        if len(content) > int(self.rules.get("heading_max_length", 60)):
            return False
        if re.search(r"[。！？.!?:：；;]$", content):
            return False
        if class_name in set((self.rules.get("heading_classes") or {}).keys()):
            return True
        return False

    def _heading_prefix(self, class_name: str) -> str:
        mapping = self.rules.get("heading_classes") or {
            "calibre2": "##",
            "calibre3": "###",
            "calibre4": "####",
            "calibre5": "####",
        }
        return mapping.get(class_name, "###")

    def _unwrap_span_content(self, text: str) -> str:
        result = []
        index = 0
        while index < len(text):
            if text[index] != "[":
                result.append(text[index])
                index += 1
                continue

            close_index = self._find_matching_bracket(text, index)
            if close_index == -1:
                result.append(text[index])
                index += 1
                continue

            class_name, class_end = self._parse_class_suffix(text, close_index + 1)
            if not class_name:
                result.append(text[index : close_index + 1])
                index = close_index + 1
                continue

            inner = self._unwrap_span_content(text[index + 1 : close_index])
            result.append(self._render_span_content(inner, class_name))
            index = class_end

        current = "".join(result).replace("\u00a0", " ")
        current = re.sub(r" {2,}", " ", current)
        return current

    def _replace_inline_span(self, match: re.Match[str]) -> str:
        content, class_name = match.groups()
        return self._render_span_content(content, class_name)

    def _replace_double_wrapped_span(self, match: re.Match[str]) -> str:
        content, inner_class, outer_class = match.groups()
        rendered = self._render_span_content(content, inner_class)
        return self._render_span_content(rendered, outer_class)

    def _render_span_content(self, content: str, class_name: str) -> str:
        lowered = class_name.lower()
        prefixes = tuple(self.rules.get("recursive_inline_classes", []))
        if lowered == "italic":
            return f"*{content}*"
        if lowered == "emphasis":
            return f"**{content}**"
        if any(lowered.startswith(prefix.lower()) for prefix in prefixes if prefix):
            return content
        return content

    def _split_quote_prefix(self, line: str) -> tuple[str, str]:
        match = re.match(r"^(\s*>\s*)?(.*)$", line)
        if not match:
            return "", line.strip()
        prefix, body = match.groups()
        return prefix or "", body.strip()

    def _unwrap_line_class_wrapper(self, text: str) -> tuple[str, str] | None:
        if not text.startswith("["):
            return None
        close_index = self._find_matching_bracket(text, 0)
        if close_index == -1:
            return None
        class_name, class_end = self._parse_class_suffix(text, close_index + 1)
        if not class_name or class_end != len(text):
            return None
        return text[1:close_index], class_name

    def _find_matching_bracket(self, text: str, start_index: int) -> int:
        depth = 0
        for index in range(start_index, len(text)):
            char = text[index]
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return index
        return -1

    def _parse_class_suffix(self, text: str, start_index: int) -> tuple[str | None, int]:
        if not text.startswith("{.", start_index):
            return None, start_index
        end_index = text.find("}", start_index)
        if end_index == -1:
            return None, start_index
        class_name = text[start_index + 2 : end_index].strip()
        if not class_name:
            return None, start_index
        return class_name, end_index + 1
