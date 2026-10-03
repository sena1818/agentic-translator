"""
导出服务
支持多种格式导出翻译结果
"""

from __future__ import annotations

import base64
import html as html_escape
import json
import mimetypes
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from markdown_it import MarkdownIt


@dataclass
class BilingualParagraph:
    """双语段落"""

    source: str
    translation: str


class ExportService:
    """导出服务"""

    # HTML 模板 - 双栏布局
    HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} - 双语对照</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC',
                         'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
            line-height: 1.8;
            color: #333;
            background: #f5f5f5;
        }}

        .container {{
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }}

        header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px 20px;
            text-align: center;
            margin-bottom: 30px;
            border-radius: 12px;
        }}

        header h1 {{
            font-size: 2rem;
            margin-bottom: 10px;
        }}

        header p {{
            opacity: 0.9;
            font-size: 1rem;
        }}

        .bilingual-row {{
            display: flex;
            gap: 20px;
            margin-bottom: 20px;
            background: white;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
        }}

        .source-col, .translation-col {{
            flex: 1;
            padding: 20px;
        }}

        .source-col {{
            background: #fafafa;
            border-right: 1px solid #eee;
            color: #666;
        }}

        .translation-col {{
            background: white;
        }}

        .source-col p, .translation-col p {{
            margin-bottom: 1em;
        }}

        .source-col p:last-child, .translation-col p:last-child {{
            margin-bottom: 0;
        }}

        .paragraph-number {{
            display: inline-block;
            background: #667eea;
            color: white;
            font-size: 12px;
            padding: 2px 8px;
            border-radius: 4px;
            margin-bottom: 10px;
        }}

        .source-col .paragraph-number {{
            background: #999;
        }}

        /* 响应式布局 */
        @media (max-width: 768px) {{
            .bilingual-row {{
                flex-direction: column;
            }}

            .source-col {{
                border-right: none;
                border-bottom: 1px solid #eee;
            }}
        }}

        /* 打印样式 */
        @media print {{
            body {{
                background: white;
            }}

            header {{
                background: #333;
                -webkit-print-color-adjust: exact;
                print-color-adjust: exact;
            }}

            .bilingual-row {{
                break-inside: avoid;
                box-shadow: none;
                border: 1px solid #ddd;
            }}
        }}

        /* Markdown 样式 */
        h1, h2, h3, h4, h5, h6 {{
            margin: 0.5em 0;
            font-weight: 600;
        }}

        h1 {{ font-size: 1.5em; }}
        h2 {{ font-size: 1.3em; }}
        h3 {{ font-size: 1.1em; }}

        strong {{ font-weight: 600; }}
        em {{ font-style: italic; }}

        a {{
            color: #667eea;
            text-decoration: none;
        }}

        a:hover {{
            text-decoration: underline;
        }}

        code {{
            background: #f0f0f0;
            padding: 2px 6px;
            border-radius: 4px;
            font-family: 'SF Mono', 'Monaco', 'Consolas', monospace;
            font-size: 0.9em;
        }}

        blockquote {{
            border-left: 3px solid #667eea;
            padding-left: 15px;
            margin: 1em 0;
            color: #666;
        }}

        img {{
            max-width: 100%;
            height: auto;
            border-radius: 4px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>{title}</h1>
            <p>双语对照阅读 | Bilingual Reading</p>
        </header>

        <main>
{content}
        </main>
    </div>
</body>
</html>
"""

    BILINGUAL_ROW_TEMPLATE = """            <div class="bilingual-row">
                <div class="source-col">
                    <span class="paragraph-number">原文 #{index}</span>
                    {source}
                </div>
                <div class="translation-col">
                    <span class="paragraph-number">译文 #{index}</span>
                    {translation}
                </div>
            </div>
"""

    @classmethod
    def parse_bilingual_markdown(cls, content: str) -> list[BilingualParagraph]:
        """
        解析双语对照 Markdown 文件

        格式:
        > 原文段落 (英文)...

        译文段落 (中文)...

        ---

        Args:
            content: Markdown 文件内容

        Returns:
            双语段落列表
        """
        paragraphs = []

        # 按分隔线分割
        sections = re.split(r"\n---+\n", content)

        for section in sections:
            section = section.strip()
            if not section:
                continue

            # 查找引用块 (原文) 和普通文本 (译文)
            # 引用块格式: > 开头的行
            lines = section.split("\n")

            source_lines = []
            translation_lines = []
            in_quote = False
            quote_ended = False

            for line in lines:
                # 检查是否是引用行
                if line.startswith(">"):
                    # 如果已经结束引用区域，这行属于译文中的引用
                    if quote_ended:
                        translation_lines.append(line)
                    else:
                        in_quote = True
                        # 移除 > 前缀
                        cleaned = line[1:].lstrip() if len(line) > 1 else ""
                        source_lines.append(cleaned)
                else:
                    # 非引用行
                    if in_quote and line.strip():
                        # 第一个非空非引用行，标记引用结束
                        quote_ended = True
                        in_quote = False
                    if quote_ended or (not in_quote and not source_lines):
                        # 已经进入译文区域，或者还没开始引用
                        if quote_ended:
                            translation_lines.append(line)

            source = "\n".join(source_lines).strip()
            translation = "\n".join(translation_lines).strip()

            # 过滤条件：
            # 1. 原文应该主要是英文（包含连续英文单词）或图片
            # 2. 译文应该主要是中文或图片
            # 3. 跳过元数据段落
            has_english_source = bool(re.search(r"[a-zA-Z]{4,}", source))
            has_chinese_translation = bool(re.search(r"[\u4e00-\u9fff]{2,}", translation))
            has_image = "![" in source or "![" in translation

            # 跳过元数据段落
            is_metadata = "由 AI 自动翻译" in source or "源文件:" in source

            if (
                source
                and translation
                and ((has_english_source and has_chinese_translation) or has_image)
                and not is_metadata
            ):
                paragraphs.append(BilingualParagraph(source=source, translation=translation))

        return paragraphs

    @classmethod
    def markdown_to_html(
        cls,
        text: str,
        markdown_path: Path | None = None,
        embed_images: bool = False,
    ) -> str:
        """
        简单的 Markdown 转 HTML

        Args:
            text: Markdown 文本

        Returns:
            HTML 文本
        """
        parser = MarkdownIt("commonmark", {"html": False}).enable("table")

        def image_rule(tokens, index, options, env):
            token = tokens[index]
            return cls._render_image_tag(token.content, token.attrGet("src") or "", markdown_path, embed_images)

        parser.renderer.rules["image"] = image_rule
        return parser.render(text)

    @classmethod
    def export_bilingual_html(cls, markdown_path: str, output_path: str = None, title: str = None) -> str:
        """
        将双语对照 Markdown 导出为 HTML

        Args:
            markdown_path: Markdown 文件路径
            output_path: 输出 HTML 路径 (默认与 Markdown 同目录)
            title: 文档标题

        Returns:
            输出文件路径
        """
        markdown_path = Path(markdown_path)

        if output_path is None:
            output_path = markdown_path.with_suffix(".html")
        else:
            output_path = Path(output_path)

        if title is None:
            title = markdown_path.stem

        # 读取 Markdown
        with open(markdown_path, encoding="utf-8") as f:
            content = f.read()

        # 解析双语段落
        records_path = markdown_path.parent / (
            markdown_path.name.replace(".bilingual.md", "").replace(".md", "") + ".json"
        )
        records = json.loads(records_path.read_text(encoding="utf-8")) if records_path.exists() else None
        paragraphs = (
            [BilingualParagraph(item["original"], item["translation"]) for item in records]
            if records is not None
            else cls.parse_bilingual_markdown(content)
        )

        # 生成 HTML 内容
        html_content = []
        for i, para in enumerate(paragraphs, 1):
            source_html = cls.markdown_to_html(
                para.source,
                markdown_path=markdown_path,
                embed_images=True,
            )
            translation_html = cls.markdown_to_html(
                para.translation,
                markdown_path=markdown_path,
                embed_images=True,
            )

            row = cls.BILINGUAL_ROW_TEMPLATE.format(index=i, source=source_html, translation=translation_html)
            html_content.append(row)

        # 生成完整 HTML
        html = cls.HTML_TEMPLATE.format(title=html_escape.escape(title), content="\n".join(html_content))

        # 写入文件
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)

        return str(output_path)

    @classmethod
    def strip_bilingual_markdown(cls, content: str) -> str:
        """
        从双语 Markdown 中提取单语译文版本。
        保留非双语元数据区块，仅移除每个双语段中的原文引用块与分隔线。
        """
        sections = re.split(r"(\n---+\n)", content)
        rendered_sections: list[str] = []

        for section in sections:
            if not section or re.fullmatch(r"\n---+\n", section):
                continue

            lines = section.split("\n")
            source_lines: list[str] = []
            translation_lines: list[str] = []
            in_quote = False
            quote_ended = False

            for line in lines:
                if line.startswith(">"):
                    if quote_ended:
                        translation_lines.append(line)
                    else:
                        in_quote = True
                        cleaned = line[1:].lstrip() if len(line) > 1 else ""
                        source_lines.append(cleaned)
                    continue

                if in_quote and line.strip():
                    quote_ended = True
                    in_quote = False

                if quote_ended:
                    translation_lines.append(line)
                elif not source_lines:
                    translation_lines.append(line)

            if source_lines and translation_lines:
                rendered_sections.append("\n".join(translation_lines).strip())
            else:
                rendered_sections.append(section.strip())

        cleaned = "\n\n".join(part for part in rendered_sections if part).strip() + "\n"
        return cls._normalize_mono_markdown(cleaned)

    @classmethod
    def _normalize_mono_markdown(cls, content: str) -> str:
        """清理从双语结果提取出的单语 Markdown。"""
        content = content.replace("（双语对照）", "")
        content = re.sub(r"^\s*---+\s*$\n?", "", content, flags=re.MULTILINE)

        lines = content.splitlines()
        normalized_lines: list[str] = []
        last_image_line = ""
        blank_streak = 0

        for line in lines:
            stripped = line.strip()

            if not stripped:
                blank_streak += 1
                if blank_streak <= 1:
                    normalized_lines.append("")
                continue

            blank_streak = 0
            if re.fullmatch(r"!\[[^\]]*\]\([^)]+\)", stripped):
                if stripped == last_image_line:
                    continue
                last_image_line = stripped
            else:
                last_image_line = ""

            normalized_lines.append(line.rstrip())

        normalized = "\n".join(normalized_lines).strip()
        return normalized + "\n"

    @classmethod
    def sync_result_assets(
        cls,
        markdown_path: str | Path,
        asset_sources: list[str | Path],
        task_id: str | None = None,
    ) -> list[str]:
        """
        将文档转换阶段提取出的图片同步到结果目录，并重写 Markdown 图片路径。
        """
        markdown_path = Path(markdown_path)
        asset_dir_name = task_id or markdown_path.stem
        target_dir = markdown_path.parent / "assets" / asset_dir_name
        target_dir.mkdir(parents=True, exist_ok=True)

        content = markdown_path.read_text(encoding="utf-8")
        image_paths = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", content)
        copied: list[str] = []

        for raw_path in image_paths:
            if raw_path.startswith(("http://", "https://", "data:")):
                continue
            filename = Path(raw_path).name
            if not filename:
                continue
            source = cls._resolve_image_source(Path(raw_path), markdown_path, asset_sources)
            if not source:
                continue
            target = target_dir / filename
            if not target.exists():
                shutil.copy2(source, target)
            copied.append(filename)

        if copied:
            rewritten = re.sub(
                r"(!\[[^\]]*\]\()([^)]+)(\))",
                lambda match: (
                    f"{match.group(1)}assets/{asset_dir_name}/{Path(match.group(2)).name}{match.group(3)}"
                    if Path(match.group(2)).name in copied
                    else match.group(0)
                ),
                content,
            )
            markdown_path.write_text(rewritten, encoding="utf-8")

        return copied

    @classmethod
    def create_assets_bundle(
        cls,
        task_id: str,
        results_dir: str | Path = "data/results",
    ) -> Path:
        """
        打包任务对应的图片资源目录。
        """
        results_dir = Path(results_dir)
        bundle_dir = results_dir / "downloads"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        zip_path = bundle_dir / f"{task_id}.assets.zip"
        assets_dir = results_dir / "assets" / task_id

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            if assets_dir.exists():
                for file_path in assets_dir.rglob("*"):
                    if file_path.is_file() and file_path.resolve().is_relative_to(assets_dir.resolve()):
                        archive.write(
                            file_path,
                            arcname=str(Path("assets") / task_id / file_path.relative_to(assets_dir)),
                        )

        return zip_path

    @classmethod
    def _render_image_tag(
        cls,
        alt: str,
        raw_path: str,
        markdown_path: Path | None,
        embed_images: bool,
    ) -> str:
        source = (
            cls._resolve_image_source(
                Path(raw_path),
                markdown_path,
                cls._candidate_asset_roots(markdown_path),
            )
            if markdown_path
            else None
        )
        src = raw_path
        if embed_images and source and source.exists():
            mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            encoded = base64.b64encode(source.read_bytes()).decode("ascii")
            src = f"data:{mime_type};base64,{encoded}"
        elif source and source.exists():
            src = source.as_posix()
        if not src.startswith(("https://", "http://", "data:image/")) and Path(src).is_absolute() and source is None:
            src = ""
        return f'<img src="{html_escape.escape(src, quote=True)}" alt="{html_escape.escape(alt or Path(raw_path).stem, quote=True)}">'

    @classmethod
    def _candidate_asset_roots(cls, markdown_path: Path | None) -> list[Path]:
        if markdown_path is None:
            return []
        task_id = markdown_path.stem
        return [
            markdown_path.parent,
            markdown_path.parent / "assets" / task_id,
            markdown_path.parent.parent / "temp" / task_id,
            markdown_path.parent.parent / "temp" / task_id / "images",
            markdown_path.parent.parent / "temp" / task_id / "images" / "images",
        ]

    @classmethod
    def _resolve_image_source(
        cls,
        image_path: Path,
        markdown_path: Path | None,
        asset_roots: list[str | Path],
    ) -> Path | None:
        allowed = [Path(root).resolve() for root in asset_roots]
        filename = image_path.name
        if image_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
            return None
        candidates = [Path(root) / image_path for root in allowed]
        if markdown_path:
            candidates.insert(0, markdown_path.parent / image_path)
        candidates += [root / filename for root in allowed]
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.is_file() and any(resolved.is_relative_to(root) for root in allowed):
                return resolved
        if not image_path.is_absolute() and ".." not in image_path.parts:
            for root in allowed:
                if root.exists():
                    for match in root.rglob(filename):
                        resolved = match.resolve()
                        if resolved.is_file() and resolved.is_relative_to(root):
                            return resolved
        return None

    @classmethod
    def export_from_pairs(cls, pairs: list[tuple[str, str]], output_path: str, title: str = "翻译结果") -> str:
        """
        从原文-译文对列表导出 HTML

        Args:
            pairs: [(原文, 译文), ...] 列表
            output_path: 输出路径
            title: 文档标题

        Returns:
            输出文件路径
        """
        html_content = []
        for i, (source, translation) in enumerate(pairs, 1):
            source_html = cls.markdown_to_html(source)
            translation_html = cls.markdown_to_html(translation)

            row = cls.BILINGUAL_ROW_TEMPLATE.format(index=i, source=source_html, translation=translation_html)
            html_content.append(row)

        html = cls.HTML_TEMPLATE.format(title=html_escape.escape(title), content="\n".join(html_content))

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)

        return output_path


# CLI 入口
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python export_service.py <markdown_file> [output_html]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    result = ExportService.export_bilingual_html(input_file, output_file)
    print(f"导出成功: {result}")
