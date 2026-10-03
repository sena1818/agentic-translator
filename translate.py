#!/usr/bin/env python3
"""
翻译系统主入口脚本

完整流程:
1. 转换 PDF/EPUB → Markdown (可选)
2. 加载术语表
3. 执行翻译
4. 保存结果
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# 添加 src 到路径
sys.path.insert(0, str(Path(__file__).parent))

from src.application.use_cases.run_translation_pipeline import RunTranslationPipeline
from src.converters.document_converter import DocumentConverter
from src.pipelines.postprocess.result_postprocess_pipeline import ResultPostprocessPipeline
from src.utils.config_loader import get_config

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    handlers=[logging.FileHandler("logs/translation.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


class TranslationPipeline:
    """翻译流水线"""

    def __init__(self, config_path: str = None):
        """
        初始化流水线

        Args:
            config_path: 配置文件路径
        """
        self.config = get_config(config_path)
        self.converter = DocumentConverter()
        self.postprocess_pipeline = ResultPostprocessPipeline()

    async def post_process_formatting(self, output_file: Path, source_type: str = "epub"):
        """
        翻译后智能格式化处理

        包括:
        - 保留并清理目录
        - 保留代码块格式
        - 转换引用块、强调等语义标记
        - 删除 Pandoc 转换残留
        - 修复图片路径

        Args:
            output_file: 输出文件路径
        """
        logger.info("🔧 应用智能格式化...")
        stats = self.postprocess_pipeline.format_markdown_file(
            output_file,
            source_type=source_type,
        )

        logger.info("🎨 格式化完成:")
        logger.info(f"  ✅ 标题标准化: {stats['headers_normalized']}")
        logger.info(f"  ✅ 引用块转换: {stats['quotes_converted']}")
        logger.info(f"  ✅ 强调转换: {stats['emphasis_converted']}")
        logger.info(f"  ✅ 代码块保留: {stats.get('code_blocks_preserved', 0)}")
        logger.info(f"  ✅ 目录保留: {stats['toc_preserved']}")
        logger.info(f"  ✅ 图片修复: {stats['images_fixed']}")
        logger.info(f"  ✅ 锚点移除: {stats['anchors_removed']}")

    def load_glossary(self, glossary_path: Path = None) -> dict:
        """
        加载术语表

        Args:
            glossary_path: 术语表路径

        Returns:
            术语表字典
        """
        if glossary_path is None:
            glossary_path = self.config.get_path("glossaries") / "glossary.json"

        if glossary_path.exists():
            with open(glossary_path, encoding="utf-8") as f:
                glossary = json.load(f)
            logger.info(f"📚 加载术语表: {len(glossary)} 个词条")
            return glossary
        else:
            logger.warning(f"⚠️ 术语表不存在: {glossary_path}")
            return {}

    def _to_markdown_file(self, input_file: Path, skip_conversion: bool) -> Path | None:
        """把输入转换成 Markdown 文件路径（已是 Markdown 则原样返回）。"""
        if skip_conversion or input_file.suffix.lower() in [".md", ".markdown"]:
            return input_file
        temp_dir = self.config.get_path("temp") / input_file.stem
        markdown_file = self.converter.convert(input_file, temp_dir)
        if markdown_file is None:
            logger.error("❌ 格式转换失败")
        return markdown_file

    async def suggest_glossary(
        self,
        input_file: Path,
        output_file: Path = None,
        glossary_path: Path = None,
        skip_conversion: bool = False,
    ):
        """术语表引导生成：扫描文档，起草候选术语表供人工审阅。"""
        from src.infrastructure.llm.chat_model_factory import ChatModelFactory
        from src.pipelines.glossary.glossary_extractor import (
            GlossaryExtractor,
            candidates_to_glossary,
        )
        from src.pipelines.translate.prompt_builder import TranslationPromptBuilder

        logger.info("=" * 60)
        logger.info("🔍 术语表引导生成")
        logger.info("=" * 60)

        markdown_file = self._to_markdown_file(input_file, skip_conversion)
        if markdown_file is None:
            return

        with open(markdown_file, encoding="utf-8") as f:
            text = f.read()

        existing = self.load_glossary(glossary_path) if glossary_path else {}

        prompt_builder = TranslationPromptBuilder(self.config)
        llm = ChatModelFactory(self.config).create_analyst()
        extractor = GlossaryExtractor(llm=llm, config=self.config, prompt_builder=prompt_builder)

        logger.info("🤖 术语专家扫描中...")
        candidates = await extractor.extract(text, existing_glossary=existing)
        if not candidates:
            logger.warning("⚠️ 未抽取到候选术语")
            return

        if output_file is None:
            output_file = self.config.get_path("glossaries") / f"{input_file.stem}_draft.json"
        output_file.parent.mkdir(parents=True, exist_ok=True)

        draft = candidates_to_glossary(candidates)
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(draft, f, ensure_ascii=False, indent=2)

        logger.info(f"📝 起草 {len(candidates)} 个候选术语:")
        for candidate in candidates:
            note = f"  # {candidate.note}" if candidate.note else ""
            logger.info(f"  {candidate.term} -> {candidate.translation}{note}")
        logger.info(f"💾 已写入草稿: {output_file}")
        logger.info("👉 请审阅、修改后作为术语表使用: -g %s", output_file)

    async def run(
        self,
        input_file: Path,
        output_file: Path = None,
        glossary_path: Path = None,
        skip_conversion: bool = False,
        skip_formatting: bool = False,
        bilingual: bool = False,
    ):
        """
        运行完整翻译流程

        Args:
            input_file: 输入文件(PDF/EPUB/MD)
            output_file: 输出文件路径
            glossary_path: 术语表路径
            skip_conversion: 跳过格式转换(直接翻译 Markdown)
            skip_formatting: 跳过格式化后处理(保留原始翻译结果)
            bilingual: 双语对照模式(原文+译文交替输出)
        """
        logger.info("=" * 60)
        logger.info("🚀 翻译流水线启动")
        logger.info("=" * 60)

        # 步骤 1: 文档转换
        if skip_conversion or input_file.suffix.lower() in [".md", ".markdown"]:
            markdown_file = input_file
            logger.info(f"📄 使用 Markdown 文件: {markdown_file}")
            source_type = "markdown"
        else:
            logger.info(f"📄 输入文件: {input_file}")
            logger.info("🔄 开始格式转换...")

            temp_dir = self.config.get_path("temp") / input_file.stem
            markdown_file = self.converter.convert(input_file, temp_dir)

            if markdown_file is None:
                logger.error("❌ 格式转换失败")
                return
            source_type = input_file.suffix.lower().lstrip(".")

        # 步骤 2: 读取文件
        logger.info(f"📖 读取文件: {markdown_file}")
        with open(markdown_file, encoding="utf-8") as f:
            text = f.read()

        # 步骤 3: 加载术语表
        glossary = self.load_glossary(glossary_path)

        # 步骤 4: 初始化翻译引擎
        logger.info("⚙️ 初始化翻译引擎...")
        translation_use_case = RunTranslationPipeline(glossary=glossary)

        # 步骤 5: 文本分块
        chunks = translation_use_case.plan_chunks(text)
        logger.info(f"✂️ 文本分块完成: {len(chunks)} 个块")

        # 步骤 6: 确定输出路径
        if output_file is None:
            output_file = self.config.get_path("output") / f"{input_file.stem}_CN.md"

        output_file.parent.mkdir(parents=True, exist_ok=True)

        # 初始化输出文件
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(f"# {input_file.stem} - 中文翻译\n\n")
            f.write("> 由 AI 自动翻译\n")
            f.write(f"> 源文件: {input_file.name}\n\n")

        # 步骤 7: 进度回调
        completed = 0
        failed = 0
        start_time = asyncio.get_event_loop().time()

        async def progress_callback(event: dict):
            nonlocal completed, failed

            if event.get("status") == "completed":
                completed += 1
                elapsed = asyncio.get_event_loop().time() - start_time
                speed = completed / elapsed * 60 if elapsed > 0 else 0

                logger.info(
                    f"✅ Chunk {event['chunk_index']} 完成 "
                    f"({completed}/{len(chunks)}, "
                    f"{completed / len(chunks) * 100:.1f}%, "
                    f"速度: {speed:.1f} chunks/分钟"
                    f"{', 缓存命中' if event.get('cached') else ''})"
                )

            elif event.get("status") == "failed":
                failed += 1
                logger.error(f"❌ Chunk {event['chunk_index']} 失败: {event.get('error')}")

        # 步骤 8: 执行翻译
        if bilingual:
            logger.info("🚀 开始翻译 (双语对照模式)...")
        else:
            logger.info("🚀 开始翻译...")
        logger.info("=" * 60)

        pipeline_output = await translation_use_case.execute(
            text=text,
            output_path=output_file,
            progress_callback=progress_callback,
            bilingual=bilingual,
            prepared_chunks=chunks,
        )
        results = pipeline_output.results
        from dataclasses import asdict

        from src.core.output_manager import OutputManager

        if not skip_formatting:
            for result in results:
                if result.success:
                    result.translation = self.postprocess_pipeline.formatter.format(
                        result.translation, source_type=source_type
                    )
        OutputManager.write_document(output_file, input_file.stem, results, bilingual)
        output_file.with_suffix(".json").write_text(
            json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2), encoding="utf-8"
        )

        asset_sources = []
        if not skip_conversion and input_file.suffix.lower() in [".pdf", ".epub", ".mobi"]:
            asset_sources = [
                temp_dir,
                temp_dir / "images",
                temp_dir / "images" / "images",
            ]
        copied_assets = self.postprocess_pipeline.sync_assets(
            markdown_paths=[output_file],
            asset_sources=asset_sources,
            task_id=output_file.stem,
        )
        if copied_assets:
            logger.info(f"🖼️ 已同步 {len(copied_assets)} 张图片到结果目录")

        # 步骤 9: 智能格式化清理
        if not skip_formatting:
            logger.info("=" * 60)
            logger.info("🎨 开始格式化处理...")
            logger.info("✅ 已完成块级格式化")

        # 步骤 10: 总结
        elapsed = asyncio.get_event_loop().time() - start_time
        logger.info("=" * 60)
        logger.info("✅ 翻译完成!")
        logger.info(f"⏱️  总耗时: {elapsed / 60:.2f} 分钟")
        logger.info(f"📈 平均速度: {len(chunks) / (elapsed / 60):.1f} chunks/分钟")
        logger.info(f"✅ 成功: {completed} 个")
        logger.info(f"❌ 失败: {failed} 个")
        logger.info(f"🔧 自动修复: {sum(1 for result in results if getattr(result, 'repaired', False))} 个")
        logger.info(f"♻️ 缓存命中: {sum(1 for result in results if getattr(result, 'cached', False))} 个")
        logger.info(f"💾 输出文件: {output_file}")
        logger.info("=" * 60)


def main():
    """命令行入口"""
    parser = argparse.ArgumentParser(description="AI 翻译系统 - 支持 PDF/EPUB/Markdown 翻译")

    parser.add_argument("input", type=Path, help="输入文件路径 (PDF/EPUB/Markdown)")

    parser.add_argument("-o", "--output", type=Path, help="输出文件路径 (默认: data/output/{filename}_CN.md)")

    parser.add_argument("-g", "--glossary", type=Path, help="术语表路径 (默认: data/glossaries/glossary.json)")

    parser.add_argument("--skip-conversion", action="store_true", help="跳过格式转换(输入已经是 Markdown)")

    parser.add_argument("--skip-formatting", action="store_true", help="跳过格式化后处理(保留原始翻译结果)")

    parser.add_argument("--bilingual", action="store_true", help="双语对照模式: 输出原文(引用块) + 译文")

    parser.add_argument(
        "--suggest-glossary",
        action="store_true",
        help="术语表引导生成: 扫描输入文档起草候选术语表到 -o (默认 data/glossaries/{filename}_draft.json)，不执行翻译",
    )

    parser.add_argument("-c", "--config", type=Path, help="配置文件路径 (默认: config/config.yaml)")

    args = parser.parse_args()

    # 检查输入文件
    if not args.input.exists():
        print(f"❌ 错误: 输入文件不存在: {args.input}")
        sys.exit(1)

    # 运行流水线
    pipeline = TranslationPipeline(config_path=args.config)

    if args.suggest_glossary:
        asyncio.run(
            pipeline.suggest_glossary(
                input_file=args.input,
                output_file=args.output,
                glossary_path=args.glossary,
                skip_conversion=args.skip_conversion,
            )
        )
        return

    asyncio.run(
        pipeline.run(
            input_file=args.input,
            output_file=args.output,
            glossary_path=args.glossary,
            skip_conversion=args.skip_conversion,
            skip_formatting=args.skip_formatting,
            bilingual=args.bilingual,
        )
    )


if __name__ == "__main__":
    main()
