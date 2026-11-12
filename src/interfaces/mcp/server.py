"""stdio MCP Server：暴露全套翻译任务管理与术语表能力。

设计原则（见 PRD #1 / issue #5）：工具处理器**直接调用既有 service 层**，
不新增业务逻辑。术语表工具与 Web 界面共享 `data/glossaries` 同一存储；
任务工具与 Web/worker 共享同一 SQLite 任务库，因此 MCP 客户端（如 Claude
Desktop）可以把本系统当作可编排的翻译能力节点。

工具清单：
- translate_text        短文本同步翻译（受术语表约束）
- submit_document       提交长文档翻译任务（异步，返回 task_id）
- get_task_status       查询任务状态与进度
- list_tasks            分页列出任务
- cancel_task           取消 pending/processing 任务
- export_result         取回结果（单语 Markdown / 双语对照）
- search_book_by_meaning 按意思检索一本书里的原话（中英文查询，只读）
- list_glossaries       列出术语表
- get_glossary          查询单个术语表
- create_glossary       新建术语表
- modify_glossary_terms 增/删术语词条
- update_glossary       全量替换术语表词条
- delete_glossary       删除术语表
"""
from __future__ import annotations

import base64

from mcp.server.fastmcp import FastMCP

from ...api.services.glossary_service import GlossaryService
from ...api.services.translation_service import TranslationService


class TranslatorMCPServer:
    """把 TranslationService / GlossaryService 封装成 MCP 工具集合。

    服务实例可注入，便于测试经 service 层接缝（临时 SQLite + FakeEngine）。
    """

    def __init__(
        self,
        translation_service: TranslationService | None = None,
        glossary_service: GlossaryService | None = None,
        name: str = "agentic-translator",
    ):
        self.translation_service = translation_service or TranslationService()
        # 复用翻译服务内部的术语表服务，保证与 Web 界面读写同一份 data/glossaries
        self.glossary_service = glossary_service or self.translation_service.glossary_service
        self.mcp = FastMCP(name)
        self._db_ready = False
        self._register_tools()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def _ensure_db(self) -> None:
        """惰性初始化任务库（stdio 进程内首个任务工具调用时执行一次）。"""
        if self._db_ready:
            return
        initialize = getattr(self.translation_service.tasks, "initialize", None)
        if initialize is not None:
            await initialize()
        self._db_ready = True

    def run(self) -> None:
        """以 stdio 传输启动 MCP Server。"""
        self.mcp.run(transport="stdio")

    # ------------------------------------------------------------------ #
    # 工具注册
    # ------------------------------------------------------------------ #
    def _register_tools(self) -> None:
        tool = self.mcp.tool
        tool(name="translate_text")(self.translate_text)
        tool(name="submit_document")(self.submit_document)
        tool(name="get_task_status")(self.get_task_status)
        tool(name="list_tasks")(self.list_tasks)
        tool(name="cancel_task")(self.cancel_task)
        tool(name="export_result")(self.export_result)
        tool(name="search_book_by_meaning")(self.search_book_by_meaning)
        tool(name="list_glossaries")(self.list_glossaries)
        tool(name="get_glossary")(self.get_glossary)
        tool(name="create_glossary")(self.create_glossary)
        tool(name="modify_glossary_terms")(self.modify_glossary_terms)
        tool(name="update_glossary")(self.update_glossary)
        tool(name="delete_glossary")(self.delete_glossary)

    # ------------------------------------------------------------------ #
    # 任务工具
    # ------------------------------------------------------------------ #
    async def translate_text(
        self,
        text: str,
        glossary_id: str | None = None,
        bilingual: bool = False,
    ) -> dict:
        """同步翻译一段短文本，立即返回译文。

        Args:
            text: 待翻译的英文文本。
            glossary_id: 可选术语表 ID，用于约束术语译名（与 Web 界面同一存储）。
            bilingual: 为 True 时在 segments 中同时返回原文-译文对照。

        Returns:
            translation 为完整中文译文；segments 为逐块的原文/译文对；
            failed_chunks 列出翻译失败的块索引。
        """
        return await self.translation_service.translate_text(
            text=text,
            glossary_id=glossary_id,
            bilingual=bilingual,
        )

    async def submit_document(
        self,
        filename: str,
        text: str | None = None,
        content_base64: str | None = None,
        glossary_id: str | None = None,
        bilingual: bool = False,
    ) -> dict:
        """提交一篇长文档进入异步翻译队列，返回可轮询的 task_id。

        文档内容二选一：Markdown/纯文本用 `text` 直接传入；PDF/EPUB 等二进制
        用 `content_base64` 传 base64 编码后的字节。任务由后台 worker 处理，
        请随后用 get_task_status 轮询、用 export_result 取回结果。

        Args:
            filename: 文件名（后缀决定是否触发文档转换，如 .pdf/.epub/.md）。
            text: 纯文本/Markdown 内容（与 content_base64 二选一）。
            content_base64: base64 编码的文件字节（与 text 二选一）。
            glossary_id: 可选术语表 ID。
            bilingual: 是否生成双语对照结果。

        Returns:
            新建任务的字典，含 task_id 与初始 status（pending）。
        """
        if text is None and content_base64 is None:
            raise ValueError("必须提供 text 或 content_base64 之一")
        if text is not None and content_base64 is not None:
            raise ValueError("text 与 content_base64 只能提供一个")

        if content_base64 is not None:
            try:
                file_content = base64.b64decode(content_base64, validate=True)
            except (base64.binascii.Error, ValueError) as exc:
                raise ValueError(f"content_base64 不是合法的 base64 编码: {exc}") from exc
        else:
            file_content = text.encode("utf-8")

        await self._ensure_db()
        task = await self.translation_service.create_task(
            file_content=file_content,
            filename=filename,
            glossary_id=glossary_id,
            bilingual=bilingual,
        )
        return task.to_dict()

    async def get_task_status(self, task_id: str) -> dict:
        """查询任务状态与进度。

        Args:
            task_id: submit_document 返回的任务 ID。

        Returns:
            任务字典，含 status、progress（current/total/percentage/speed/elapsed）、
            result_url、error 等字段。
        """
        await self._ensure_db()
        task = await self.translation_service.get_task(task_id)
        if not task:
            raise ValueError(f"任务不存在: {task_id}")
        return task.to_dict()

    async def list_tasks(self, skip: int = 0, limit: int = 20) -> dict:
        """分页列出翻译任务（按创建时间倒序）。

        Args:
            skip: 跳过的记录数。
            limit: 返回条数上限。

        Returns:
            含 tasks 列表与 total 总数的字典。
        """
        await self._ensure_db()
        tasks, total = await self.translation_service.list_tasks(skip, limit)
        return {"tasks": [t.to_dict() for t in tasks], "total": total}

    async def cancel_task(self, task_id: str) -> dict:
        """取消一个 pending 或 processing 任务。

        对已进入终态（completed/partial_success/failed/cancelled）的任务无效。

        Args:
            task_id: 待取消的任务 ID。

        Returns:
            含 cancelled（是否发生取消）与 status（取消后任务状态）的字典。
        """
        await self._ensure_db()
        result = await self.translation_service.cancel_task(task_id)
        if result is None:
            raise ValueError(f"任务不存在: {task_id}")
        task = await self.translation_service.get_task(task_id)
        return {
            "task_id": task_id,
            "cancelled": result,
            "status": task.status.value if task else None,
        }

    async def export_result(self, task_id: str, variant: str = "mono") -> dict:
        """取回翻译结果内容。

        Args:
            task_id: 任务 ID。
            variant: "mono" 取单语译文，"bilingual" 取双语对照 Markdown。

        Returns:
            含 available（结果是否已生成）、content（Markdown 文本）、status 的字典。
        """
        if variant not in {"mono", "bilingual"}:
            raise ValueError("variant 只能是 'mono' 或 'bilingual'")
        await self._ensure_db()
        result = await self.translation_service.export_result(task_id, variant)
        if result is None:
            raise ValueError(f"任务不存在: {task_id}")
        return result

    async def search_book_by_meaning(self, task_id: str, query: str, limit: int = 10) -> dict:
        """按意思检索一本已翻译的书：用一句大概的话（中文或英文）找到意思相近的段落。

        原文和译文都参与匹配；返回每段的原文、译文、seg_id（对照阅读器段号，可用于跳转）与排序信号。
        只读，不修改任何数据。未配置向量模型时为仅关键词排序，mode 为 "lexical" 并在 hint 中说明。

        Args:
            task_id: 已完成的结构化翻译任务 ID。
            query: 查询句子，中文或英文均可。
            limit: 返回条数（1~50）。
        """
        if not query.strip():
            raise ValueError("query 不能为空")
        await self._ensure_db()
        return await self.translation_service.search_meaning(task_id, query, max(1, min(limit, 50)))

    # ------------------------------------------------------------------ #
    # 术语表工具（与 Web 界面共享 data/glossaries 存储）
    # ------------------------------------------------------------------ #
    async def list_glossaries(self) -> list[dict]:
        """列出所有术语表（id / name / term_count / updated_at）。"""
        return await self.glossary_service.list_glossaries()

    async def get_glossary(self, glossary_id: str) -> dict:
        """查询单个术语表的全部词条。

        Args:
            glossary_id: 术语表 ID。
        """
        glossary = await self.glossary_service.get_glossary(glossary_id)
        if not glossary:
            raise ValueError(f"术语表不存在: {glossary_id}")
        return glossary

    async def create_glossary(self, name: str, terms: dict[str, str]) -> dict:
        """新建术语表。

        Args:
            name: 术语表名称（将转为小写下划线 id）。
            terms: 术语映射，如 {"Hyperstition": "超虚构 (Hyperstition)"}。
        """
        return await self.glossary_service.create_glossary(name, terms)

    async def modify_glossary_terms(
        self,
        glossary_id: str,
        add: dict[str, str] | None = None,
        remove: list[str] | None = None,
    ) -> dict:
        """增量增删术语词条（在对话中即时修正译名）。

        Args:
            glossary_id: 术语表 ID。
            add: 新增/覆盖的词条映射。
            remove: 待删除的英文词条键列表。

        Returns:
            含 term_count（修改后词条总数）的字典。
        """
        result = await self.glossary_service.modify_terms(glossary_id, add, remove)
        if result is None:
            raise ValueError(f"术语表不存在: {glossary_id}")
        return {"glossary_id": glossary_id, "term_count": result}

    async def update_glossary(self, glossary_id: str, terms: dict[str, str]) -> dict:
        """全量替换术语表词条。

        Args:
            glossary_id: 术语表 ID。
            terms: 新的完整词条映射（覆盖原有全部词条）。
        """
        success = await self.glossary_service.update_glossary(glossary_id, terms)
        if not success:
            raise ValueError(f"术语表不存在: {glossary_id}")
        return {"glossary_id": glossary_id, "term_count": len(terms)}

    async def delete_glossary(self, glossary_id: str) -> dict:
        """删除整个术语表。

        Args:
            glossary_id: 术语表 ID。
        """
        success = await self.glossary_service.delete_glossary(glossary_id)
        if not success:
            raise ValueError(f"术语表不存在: {glossary_id}")
        return {"glossary_id": glossary_id, "deleted": True}


def build_server() -> TranslatorMCPServer:
    """构造默认（生产）MCP Server 实例。"""
    return TranslatorMCPServer()


def main() -> None:
    """console entry point：启动 stdio MCP Server。"""
    build_server().run()


if __name__ == "__main__":
    main()
