"""
pandoc 进程适配器：Markdown ⇄ JSON AST，AST → HTML 片段。

所有调用都走 stdin/stdout，不落临时文件；失败抛 PandocError。
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess

READER = "markdown"
# 输出 Markdown 时：关闭 smart（直接写 “”、——，而不是 "、--- 这类 pandoc 专有转义，GitHub 等渲染器才能正确显示）；
# 禁用 simple/multiline/grid 表格，让 pandoc 回落到 GFM 风格的管道表格
WRITER = "markdown-smart-simple_tables-multiline_tables-grid_tables"
BREAK_MARKER = "<!--at-chunk-break-->"
_FOOTNOTE_LABEL_RE = re.compile(r"\[\^([^\]\s]+)\]")


class PandocError(RuntimeError):
    pass


@functools.lru_cache(maxsize=1)
def pandoc_available() -> bool:
    return shutil.which("pandoc") is not None


def _run(args: list[str], payload: str) -> str:
    if not pandoc_available():
        raise PandocError("pandoc 未安装")
    completed = subprocess.run(
        ["pandoc", *args],
        input=payload.encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=300,
    )
    if completed.returncode != 0:
        raise PandocError(completed.stderr.decode("utf-8", "replace")[:500])
    return completed.stdout.decode("utf-8")


def markdown_to_ast(text: str) -> dict:
    return json.loads(_run(["-f", READER, "-t", "json"], text))


def _document(ast_template: dict, blocks: list[dict]) -> str:
    return json.dumps(
        {"pandoc-api-version": ast_template["pandoc-api-version"], "meta": {}, "blocks": blocks},
        ensure_ascii=False,
    )


def blocks_to_markdown(ast_template: dict, blocks: list[dict]) -> str:
    """把块列表写成 Markdown；脚注定义紧随引用所在块，便于按块切分。"""
    output = _run(
        ["-f", "json", "-t", WRITER, "--wrap=none", "--reference-location=block"],
        _document(ast_template, blocks),
    )
    return output.strip()


def document_to_markdown(ast: dict) -> str:
    """整篇文档写成 Markdown，保留 YAML 元数据（标题、作者等）。"""
    return _run(
        ["-f", "json", "-t", WRITER, "--standalone", "--wrap=none"],
        json.dumps(ast, ensure_ascii=False),
    ).strip()


def blocks_groups_to_markdown(ast_template: dict, groups: list[list[dict]]) -> list[str]:
    """一次 pandoc 调用渲染多组块（以原始注释分隔），返回每组的 Markdown。"""
    if not groups:
        return []
    marker = {"t": "RawBlock", "c": ["html", BREAK_MARKER]}
    blocks: list[dict] = []
    for index, group in enumerate(groups):
        if index:
            blocks.append(marker)
        blocks.extend(group)
    output = blocks_to_markdown(ast_template, blocks)
    parts = [part.strip() for part in output.split(BREAK_MARKER)]
    if len(parts) != len(groups):
        raise PandocError("分组渲染结果数量不匹配")
    return parts


def markdown_groups_to_blocks(texts: list[str]) -> list[list[dict]]:
    """一次 pandoc 调用解析多段 Markdown，返回每段的块列表。

    各段脚注标签加上段前缀，避免不同段的 [^1] 互相冲突。
    """
    if not texts:
        return []
    pieces = []
    for index, text in enumerate(texts):
        defined = set(re.findall(r"(?m)^\[\^([^\]\s]+)\]:", text))
        pieces.append(
            _FOOTNOTE_LABEL_RE.sub(
                lambda m, i=index, d=defined: f"[^g{i}-{m.group(1)}]" if m.group(1) in d else m.group(0), text
            )
        )
    joined = f"\n\n{BREAK_MARKER}\n\n".join(pieces)
    blocks = markdown_to_ast(joined)["blocks"]
    groups: list[list[dict]] = [[]]
    for block in blocks:
        if block["t"] == "RawBlock" and block["c"][1].strip() == BREAK_MARKER:
            groups.append([])
        else:
            groups[-1].append(block)
    if len(groups) != len(texts):
        raise PandocError("分组解析结果数量不匹配")
    return groups


def blocks_to_html(ast_template: dict, blocks: list[dict], id_prefix: str) -> str:
    """把块列表渲染为 HTML 片段：MathML 公式（离线可用），脚注按章节就近放置。"""
    return _run(
        [
            "-f",
            "json",
            "-t",
            "html5",
            "--mathml",
            "--wrap=none",
            "--reference-location=section",
            f"--id-prefix={id_prefix}",
        ],
        _document(ast_template, blocks),
    )


def blocks_to_epub(
    ast_template: dict,
    blocks: list[dict],
    output_path: str,
    *,
    title: str,
    lang: str = "zh-CN",
    resource_paths: list[str] | None = None,
) -> None:
    """把块列表写成 EPUB3：按一级标题分章、自动目录、脚注带回链、MathML 公式。

    图片路径应已解析为本地绝对路径（或 resource_paths 下的相对路径），pandoc 会把图片打进包里。
    """
    args = [
        "-f",
        "json",
        "-t",
        "epub3",
        "--toc",
        "--toc-depth=3",
        "--mathml",
        f"--metadata=lang:{lang}",
        "-o",
        output_path,
    ]
    meta = dict(ast_template.get("meta") or {})
    if "title" not in meta:  # 源文 YAML 里有标题时沿用，否则用任务文件名
        args.append(f"--metadata=title:{title}")
    if resource_paths:
        args.append("--resource-path=" + os.pathsep.join(resource_paths))
    payload = {"pandoc-api-version": ast_template["pandoc-api-version"], "meta": meta, "blocks": blocks}
    _run(args, json.dumps(payload, ensure_ascii=False))
