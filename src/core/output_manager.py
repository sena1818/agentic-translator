"""
顺序输出管理器
从 scripts/async_translator.py:422-478 提取
解决异步并发导致的乱序输出问题
"""

import asyncio
import re

try:
    import aiofiles
except ImportError:  # pragma: no cover - 仅在瘦测试环境退化为同步写入
    aiofiles = None


class OutputManager:
    """
    输出管理器

    核心功能:
    - 缓冲异步完成的结果
    - 按索引顺序写入文件
    - 失败块写入占位符,不阻塞后续
    - 支持双语对照输出模式
    """

    def __init__(self, output_path: str, start_index: int = 0, bilingual: bool = False):
        """
        初始化输出管理器

        Args:
            output_path: 输出文件路径
            start_index: 起始索引(用于断点续传)
            bilingual: 是否启用双语对照模式
        """
        self.output_path = output_path
        self.buffer = {}  # {index: (content, original)}
        self.next_index = start_index
        self.lock = asyncio.Lock()
        self.written_count = 0
        self.bilingual = bilingual

    async def add_result(self, index: int, content: str, success: bool, original_text: str = ""):
        """
        添加翻译结果

        Args:
            index: 块索引
            content: 翻译内容
            success: 是否成功
            original_text: 原文(用于失败占位符和双语对照)
        """
        async with self.lock:
            final_content = self.render_result(index, content, success, original_text, self.bilingual)

            # 2. 存入缓冲区
            self.buffer[index] = final_content

            # 3. 尝试连续写入 (Flush Buffer)
            if aiofiles is None:
                with open(self.output_path, "a", encoding="utf-8") as f:
                    while self.next_index in self.buffer:
                        text_to_write = self.buffer[self.next_index]
                        f.write("\n\n")
                        f.write(text_to_write)
                        f.write("\n\n")
                        del self.buffer[self.next_index]
                        self.next_index += 1
                        self.written_count += 1
            else:
                async with aiofiles.open(self.output_path, "a", encoding="utf-8") as f:
                    while self.next_index in self.buffer:
                        text_to_write = self.buffer[self.next_index]

                        await f.write("\n\n")
                        await f.write(text_to_write)
                        await f.write("\n\n")

                        # 清理内存
                        del self.buffer[self.next_index]
                        self.next_index += 1
                        self.written_count += 1

    @staticmethod
    def _format_bilingual(original: str, translation: str) -> str:
        """
        格式化双语对照内容

        Args:
            original: 原文
            translation: 译文

        Returns:
            格式化后的双语对照文本
        """
        translation_image_paths = set(re.findall(r"!\[[^\]]*\]\(([^)]+)\)", translation))
        original_lines = []
        for line in original.strip().split("\n"):
            image_match = re.fullmatch(r"!\[[^\]]*\]\(([^)]+)\)", line.strip())
            if image_match and image_match.group(1) in translation_image_paths:
                continue
            original_lines.append(line)

        quoted_original = "\n".join(f"> {line}" for line in original_lines if line or len(original_lines) == 1)
        if quoted_original:
            return f"{quoted_original}\n\n{translation}\n\n---"
        return f"{translation}\n\n---"

    @classmethod
    def render_result(cls, index, content, success, original_text="", bilingual=False):
        if success:
            return cls._format_bilingual(original_text, content) if bilingual else content
        return (
            f"\n\n> **[翻译失败 - Chunk {index}]**\n> 请重试此块或人工补全。\n\n```text\n{original_text[:500]}\n```\n"
        )

    @classmethod
    def write_document(cls, path, filename, results, bilingual=False):
        mode = "（双语对照）" if bilingual else ""
        parts = [f"# {filename} - 中文翻译{mode}\n\n> 由 AI 自动翻译"]
        for result in sorted(results, key=lambda item: item.chunk_index):
            parts.append(
                cls.render_result(result.chunk_index, result.translation, result.success, result.original, bilingual)
            )
        path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")

    @property
    def current_index(self) -> int:
        """当前应写入的索引"""
        return self.next_index

    @property
    def buffer_size(self) -> int:
        """缓冲区大小"""
        return len(self.buffer)
