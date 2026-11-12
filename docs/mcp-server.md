# MCP Server（stdio）

把本翻译系统暴露为一个 [Model Context Protocol](https://modelcontextprotocol.io) 服务，
让 Claude Desktop 等 MCP 客户端把它当作可编排的**翻译能力节点**：短文本即时翻译、
提交长文档任务并轮询、管理术语表——全部无需打开 Web 界面。

工具处理器直接调用既有 service 层（`TranslationService` / `GlossaryService`），
不新增业务逻辑，因此与 Web 界面、后台 worker **共享同一份 SQLite 任务库和
`data/glossaries` 术语表存储**。

**工具一套、出口两个**：所有工具先进同一个 `ToolRegistry`（#15 的 agent 运行框架用的那个），
再整体挂到 MCP。内部 agent 取工具子集时拿到的就是 MCP 暴露的同一批函数对象，同一段实现
（见下文"给内部 agent 挂工具"）。

## 工具清单

### 任务与术语表

| 工具 | 作用 |
| --- | --- |
| `translate_text` | 短文本同步翻译，立即返回译文（可选术语表约束） |
| `submit_document` | 提交长文档进入异步翻译队列，返回 `task_id` |
| `get_task_status` | 查询任务状态与进度 |
| `list_tasks` | 分页列出任务 |
| `cancel_task` | 取消 `pending` / `processing` 任务 |
| `export_result` | 取回结果（`mono` 单语 / `bilingual` 双语对照 Markdown） |
| `list_glossaries` | 列出术语表 |
| `get_glossary` | 查询单个术语表词条 |
| `create_glossary` | 新建术语表 |
| `modify_glossary_terms` | 增量增/删术语词条 |
| `update_glossary` | 全量替换术语表词条 |
| `delete_glossary` | 删除术语表 |

### 审校（`src/application/tools/review.py`）

| 工具 | 作用 | 写入 |
| --- | --- | :---: |
| `list_quality_issues` | 列出质检问题（含失败块），按严重度排序，可按 severity / kind 过滤 | |
| `read_chunk` | 读一块的原文、译文、质检报告和当前 `revision` | |
| `read_segment` | 读一段（对照阅读器段号）的原文、译文和该段的问题 | |
| `edit_chunk` | 修改一块译文；预览返回新旧译文与新译文的质检结果 | ✅ |
| `retranslate_chunks` | 把指定块重新排队翻译 | ✅ |
| `resume_task` | 只重译失败的块 | ✅ |

### 术语与记忆（`src/application/tools/terms.py`）

| 工具 | 作用 | 写入 |
| --- | --- | :---: |
| `term_occurrences` | 一个词在全书原文里的全部出现处（含所在段的译文） | |
| `term_renderings` | 一个词在全书的各种译法、次数与证据段 | |
| `read_term_ledger` | 本书当前生效的术语（术语表 + 账本 + 人工决定），标明来源 | |
| `update_term_decision` | 定下一个术语的译名（不改已有译文） | ✅ |
| `propagate_term_correction` | 把旧译名改成新译名并传播到全书；预览列出要改的段 | ✅ |
| `list_corrections` | 本书的纠正记录（编辑、术语决定、传播） | |
| `term_suggestions` | 同作者 / 同领域其他书的术语建议 | |

### 审计、用量与检索（`audit.py`、`search.py`）

| 工具 | 作用 |
| --- | --- |
| `run_nontext_audit` | 对当前译文重跑全书非文字审计（不调用模型、不写文件），可按来源过滤 |
| `quality_summary` | 任务的质量汇总计数 |
| `usage_report` | 各块耗时与重试、可选阶段（术语账本、语义质检、分类型修复）的用量；翻译调用的 token 未按任务记录，见 Langfuse |
| `search_book` | 按关键词查段（原文 / 译文 / 任一） |
| `search_book_by_meaning` | 按意思找原话（中英文查询） |

### 写入类工具：先预览、再确认

上表标"写入"的工具都有 `confirm` 参数，默认 `false`：只返回预览（要改什么、改后什么样、改后的质检结果），
**不改变任何数据**；确认无误后带 `confirm: true` 再调用一次才执行。编辑和传播会带上预览时的 `revision`，
期间结果被别人改过则拒绝执行，需重新预览。

（任务与术语表那一组是先前就有的工具，保持原行为：`submit_document`、`cancel_task` 与术语表增删改
调用即生效。）

**例：把一个术语的译名改掉并传播到全书**

1. `term_renderings(task_id, "Numogram")` → 看到全书都译成"数图"；
2. `update_term_decision(task_id, "Numogram", "数字图")` 预览 → 带 `confirm: true` 确认；
3. `propagate_term_correction(task_id, "Numogram", "数图", "数字图")` 预览 → 列出要改的段与改后文字；
4. 同一工具带 `revision`（预览返回的）和 `confirm: true` → 应用；改动的块按新译名重新质检。

## 给内部 agent 挂工具

工具写成带 `@shared_tool` 的 async 函数，第一个参数是 `ctx: ToolContext`，其余参数带类型注解
（参数 schema 由注解推出，内部注册表按它校验，FastMCP 也从同一签名生成输入 schema）：

```python
from src.application.tools import ToolContext, shared_tool

@shared_tool("lookup_sense")
async def lookup_sense(ctx: ToolContext, task_id: str, term: str, limit: int = 5) -> dict:
    """查一个术语在书中的几种义项。（docstring 第一段就是工具说明）"""
    ...

@shared_tool("set_sense", writes=True)          # 写入类必须带 confirm，默认只预览
async def set_sense(ctx: ToolContext, task_id: str, term: str, sense: str, confirm: bool = False) -> dict:
    ...
```

- **同时暴露给 MCP**：把模块加进 `src/application/tools/__init__.py` 的 `DEFAULT_MODULES`。
- **只给内部 agent 用**：不加进 `DEFAULT_MODULES`，组装时单独传入：

```python
from src.application.tools import ToolContext, build_tool_suite
from src.application.tools import terms, review
from . import my_agent_tools

suite = build_tool_suite(ToolContext(service=translation_service), modules=[terms, review, my_agent_tools])
registry = suite.subset(["term_occurrences", "term_renderings", "read_segment", "lookup_sense"])
# registry 直接交给 src/infrastructure/agent 的运行器
```

`ToolContext` 带 `service`（任务用例）、`glossary_service`、`extras`（模块自己需要的其他依赖，
如翻译前还没有任务时传入的全书索引），以及每次调用前执行的 `prepare` 钩子。
已在 MCP server 里的注册表可直接复用：`TranslatorMCPServer(...).registry`。

### 典型工作流

**短文本**：直接 `translate_text`。

**长文档**：`submit_document` → 轮询 `get_task_status` 直到 `completed` /
`partial_success` → `export_result` 取回 Markdown。文档内容二选一：纯文本 /
Markdown 用 `text` 直接传；PDF / EPUB 等二进制用 `content_base64` 传 base64 字节。

**取消语义**：`cancel_task` 只对 `pending` / `processing` 任务生效并置为
`cancelled`；对已进入终态的任务返回 `cancelled: false` 且不改变状态。取消一个
运行中的任务后，即使后台 worker 把该批次跑完，也不会把状态覆盖回 `completed`。

## 运行

MCP Server 走 **stdio** 传输——由客户端负责拉起进程，通常不需要手动启动。
本地调试可直接运行：

```bash
# 免安装（在仓库根目录运行，data/ 相对路径就地解析）
python -m src.interfaces.mcp.server

# 或安装为 console 命令后
pip install -e .
translator-mcp
```

翻译需要 LLM 密钥，请确保 `.env` 中已配置 `SILICONFLOW_API_KEY`（见
[快速开始](../README.md)）。术语表工具与任务查询不消耗 LLM。

## 在 Claude Desktop 中配置

编辑 Claude Desktop 的 `claude_desktop_config.json`
（macOS 路径：`~/Library/Application Support/Claude/claude_desktop_config.json`），
新增一个 MCP server：

```json
{
  "mcpServers": {
    "agentic-translator": {
      "command": "python",
      "args": ["-m", "src.interfaces.mcp.server"],
      "cwd": "/absolute/path/to/agentic-translator",
      "env": {
        "SILICONFLOW_API_KEY": "sk-your-key-here"
      }
    }
  }
}
```

- `cwd` 必须指向仓库根目录，服务内部的 `data/` 相对路径（任务库、上传、结果、
  术语表）都以此为基准。
- 已执行 `pip install -e .` 时，可把 `command` 换成 `translator-mcp` 并省略 `args`。
- 保存后重启 Claude Desktop，即可在对话中看到并调用上述 12 个工具。

## 测试

工具处理器经 service 层接缝测试（临时 SQLite + 注入符合引擎契约的
`FakeEngine`，不触碰真实 LLM），覆盖工具发现、术语约束、`submit → status →
export` 全流程、取消状态语义、术语表 CRUD 与存储互通：

```bash
pytest tests/test_mcp_server.py -q
```
