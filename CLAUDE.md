# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 提供在此代码库中工作的指导。

## 请总是使用中文来回复

---

## 📚 项目概览

**面向术语密集型书籍与论文的 Agentic 翻译系统**：输入 PDF / EPUB / Markdown，输出可审校的中文 Markdown（可选双语对照）。不绑定领域，靠自定义术语表 + 可配置 Prompt 适配（后现代哲学如 Nick Land 的 CCRU 著作、Cyclonopedia，机器学习论文等）。

**核心特性**：
- 🕸️ 文档级 LangGraph StateGraph 编排三个 LLM 角色：分析员 → 翻译员 → 审校员（ADR-0001）
- 🧩 章节感知分块，稳定 `chunk_id`，携带章节路径与上一块末尾语境
- 📚 术语表按块筛选注入 Prompt，并由规则质检校验
- 🛡️ 规则质检 + 选择性修复：只有不合格的块才调用审校员
- 💾 chunk 级 SQLite 翻译缓存承担断点续传；顺序流式写盘
- ⚠️ 失败块写占位符，不阻塞整本书
- 🌐 React + FastAPI Web 界面，SQLite 任务队列（租约 / 执行版本 / 心跳），逐块人工审校与重译（ADR-0003）
- 🔌 MCP stdio server、🔭 Langfuse 可观测（可降级）、📏 `translator-eval` 评测框架

领域术语以 [CONTEXT.md](CONTEXT.md) 为准；架构决策见 `docs/adr/`。

---

## 🏗️ 核心架构

### 翻译流水线

```
入口：translate.py (CLI) / src/api (FastAPI + worker) / src/interfaces/mcp (MCP)
  ↓
文档转换  PDF → marker_single，EPUB/MOBI → pandoc（src/infrastructure/converters）
  ↓
残留清理  EpubArtifactCleaner，规则见 config/artifact_rules.yaml（src/pipelines/preprocess）
  ↓
章节感知分块  ChunkPlanner（src/domain/rules/chunk_planning.py）
  ↓
LangGraph 文档图（src/pipelines/translate/graph_engine.py）
  START → analyze（分析员：首/中/尾采样 → DocumentProfile）
        → Send API fan-out（每块一个分支，max_concurrency 控制并发）
            分支内：种子结果/缓存命中？→ 翻译员 → 规则质检 → (不过) 审校员修复 → 复检
            → OutputManager 按原文顺序流式写盘
        → aggregate（仅汇总）→ END
  ↓
后处理  SmartMarkdownFormatter + 图片资源同步（src/pipelines/postprocess）
  ↓
产物  单语/双语 Markdown、双栏 HTML、逐块结构化记录（含质检报告）
```

### 关键组件

| 组件 | 位置 | 要点 |
|---|---|---|
| 翻译引擎宿主 | `src/core/translator.py` | 组装各组件；缓存键 `_build_cache_key`；两引擎共用 `_translate_and_emit` |
| 分块器 | `src/domain/rules/chunk_planning.py` | 按标题栈切块，代码围栏不拆，超长块按分隔符逐级降级切分，短块合并 |
| 术语筛选 | `src/domain/rules/glossary_selection.py` | 只注入本块正文+上文中出现的术语；同时用于缓存键 |
| 分析员 | `src/pipelines/translate/document_analyzer.py` | 产出 JSON 文档画像，失败退化为空画像；画像按文档指纹缓存 |
| 翻译员 | `src/pipelines/translate/translation_client.py` | LCEL 链；4xx（除 408/409/429）不重试，其余指数退避 |
| 质检/修复 | `src/core/validator.py` + `src/pipelines/translate/quality_pipeline.py` | 纯规则质检；按严重度加权择优，`max_fix_attempts` 次修复 |
| Prompt | `src/pipelines/translate/prompt_builder.py` | 角色与要求可由配置 `prompt:` 段覆盖（示例 `config/research_paper.yaml`） |
| 顺序输出 | `src/core/output_manager.py` | buffer + next_index，乱序完成、顺序落盘；失败占位符 |
| 限流 | `src/infrastructure/llm/rate_limiter.py`、`request_budget.py` | 进程内令牌桶 + 跨 worker 共享 SQLite 并发租约与滚动 RPM |
| 模型工厂 | `src/infrastructure/llm/chat_model_factory.py` | 三角色模型；SDK 重试关闭（`max_retries=0`），挂 Langfuse 回调 |
| 任务用例 | `src/application/use_cases/translation_tasks.py` | 配置/术语表快照、租约心跳、私有执行目录、CAS 发布产物、人工审校 |
| 任务存储 | `src/infrastructure/persistence/database.py` | `BEGIN IMMEDIATE` 认领；更新校验 execution_id + 租约 |
| 术语起草 | `src/pipelines/glossary/glossary_extractor.py` | 术语专家 agent 起草候选术语表 |
| 评测 | `src/evaluation/` | LLM-as-judge（Gemini 裁判）、三组对照、译法一致率（ADR-0002） |

编排引擎开关：`multi_agent.engine: langgraph | native`。`native`（`batch_orchestrator.py`）为过渡期对照组，两者由 `tests/test_engine_equivalence.py` 保证逐字段等价。修改单块处理逻辑时改 `_translate_and_emit`，不要只改其中一条路径。

---

## 📁 目录结构

```
agentic-translator/
├── translate.py                 # ⭐ CLI 主入口
├── run_server.py                # FastAPI（默认带内联 worker），:8000
├── run_worker.py                # 独立 worker 集群
├── src/
│   ├── domain/                  # 纯逻辑无 I/O：models、rules（分块、术语筛选、文档采样）
│   ├── application/use_cases/   # 翻译用例、任务状态迁移与审校
│   ├── pipelines/
│   │   ├── preprocess/          # EPUB/Pandoc 残留清理
│   │   ├── translate/           # graph_engine、batch_orchestrator、prompt/client/quality/analyzer
│   │   ├── glossary/            # 术语表起草
│   │   └── postprocess/         # Markdown 格式化、导出（双语 HTML 等）
│   ├── infrastructure/          # llm、cache、persistence、converters、observability、filesystem
│   ├── interfaces/mcp/          # MCP stdio server
│   ├── evaluation/              # translator-eval
│   ├── api/                     # FastAPI app、routes、worker；services/ database/ 为兼容导出
│   ├── core/                    # translator、validator、output_manager；其余为兼容导出
│   ├── converters/              # 兼容导出 → infrastructure/converters
│   └── utils/config_loader.py
├── config/
│   ├── config.yaml              # 主配置
│   ├── research_paper.yaml      # 科研论文配置示例（-c 指定）
│   └── artifact_rules.yaml      # 转换残留清理规则（common/epub/mobi/kindle/pandoc/pdf，支持 inherit_from）
├── data/
│   ├── glossaries/              # glossary.json、CPglossary.json、research_paper_glossary.json、demo.json
│   ├── translation_cache.db     # chunk 翻译缓存 + 文档画像缓存（运行时生成）
│   ├── translation.db           # Web 任务队列（运行时生成）
│   ├── request_budget.db        # 跨 worker 请求预算（运行时生成）
│   ├── uploads/ results/        # Web 上传文件与任务产物（results/runs/{task_id}/{execution_id}）
│   └── input/ output/           # CLI 默认输入/输出目录
├── frontend/                    # React 19 + Vite + antd + react-query
├── tests/                       # pytest（含引擎等价、可靠性回归、MCP、评测）
├── docs/                        # adr/、evals/、guides/、mcp-server.md、observability.md
├── scripts/                     # generate_api_types.py、longrun.sh、fixpath.py 等
├── logs/translation.log         # CLI / API / worker 日志
├── Dockerfile / docker-compose.yml
└── .env.example
```

`src/core/chunk_planner.py`、`translation_cache.py`、`rate_limiter.py`、`src/converters/`、`src/api/services/`、`src/api/database/` 只是兼容旧导入路径的再导出，新代码请直接引用 `domain/`、`infrastructure/`、`application/` 下的真实实现。

---

## ⚡ 常用命令

### CLI 翻译

```bash
# PDF/EPUB 自动转换 + 翻译（默认输出 data/output/{filename}_CN.md）
python translate.py BookTrans/Cyclonopedia.epub -g data/glossaries/CPglossary.json -o output_final/Cyclonopedia_CN.md

# 已是 Markdown，跳过转换
python translate.py input.md -o output.md --skip-conversion

# 双语对照
python translate.py input.md --bilingual --skip-conversion

# 只起草术语表、不翻译（默认写到 data/glossaries/{filename}_draft.json）
python translate.py input.md --suggest-glossary --skip-conversion

# 使用领域配置
python translate.py paper.md -c config/research_paper.yaml -g data/glossaries/research_paper_glossary.json --skip-conversion
```

其他参数：`--skip-formatting`（跳过格式化后处理）。

### Web / Worker / Docker

```bash
python run_server.py                                   # API + 内联 worker，:8000
TRANSLATION_INLINE_WORKER=0 python run_server.py       # 只启动 API
python run_worker.py --processes 4 --parallel-tasks 2  # 独立 worker 集群
cd frontend && npm install && npm run dev              # 前端开发服务器 :5173
docker compose up --build                              # 前端 + API + worker；PDF 支持需 ENABLE_PDF=1 docker compose build
```

### MCP / 评测

```bash
python -m src.interfaces.mcp.server          # 或 translator-mcp
translator-eval --dry-run                    # 假模型冒烟验证评测框架
translator-eval --out docs/evals/results     # 真实评测，需 SILICONFLOW_API_KEY + GOOGLE_API_KEY
```

### 开发检查（提交前运行）

```bash
ruff check .
pytest
python scripts/generate_api_types.py --check   # 前后端 API 类型契约
cd frontend && npm ci && npm run lint && npm test && npm run build
```

### 断点续传与日志

- 断点续传是自动的：重跑同一命令时，已成功的块会命中 `data/translation_cache.db`，只重新翻译未命中或失败的块。
- 缓存键包含 prompt 版本与指纹、模型、API 地址、chunk_id、上文语境、质检配置、文档画像指纹和本块相关的术语。修改其中任何一项，相关块都会自动重译，**不需要手动清缓存**。
- 要强制全部重译，可以删除 `data/translation_cache.db`。注意这会清空**所有**文档的缓存。
- 查看实时日志：`tail -f logs/translation.log`

---

## ⚙️ 配置

全部配置在 `config/config.yaml`（可用 `-c` 指定其他文件）。常用项：

```yaml
api:
  model: "deepseek-ai/DeepSeek-V3"   # 经 SiliconFlow 的 OpenAI 兼容端点
  translator: { temperature: 0.3, max_retries: 3 }
  checker:    { temperature: 0.1 }

concurrency:
  max_concurrent_requests: 10    # 同一账号跨所有 worker 共享
  rate_limit_per_minute: 200
  request_timeout: 60

text_splitting:
  chunk_size: 3600               # 块上限（字符）
  target_chunk_size: 3200
  min_chunk_size: 1200           # 短于此值的块并入同章节前一块
  context_window: 1400           # 上一块末尾作为上文语境的字符数

quality:
  enable_qa_check: true
  max_fix_attempts: 1
  untranslated_word_span: 12     # 连续多少个英文单词判为未翻译
  max_glossary_checks: 25        # 每块最多校验多少个术语（按术语长度降序）

multi_agent:
  enabled: true                  # 关闭则不运行分析员
  engine: "langgraph"            # langgraph | native
  analyst_max_chars: 12000       # 分析员采样预算

worker:
  stale_after_seconds: 900       # 任务租约时长
  max_parallel_tasks: 1

observability:
  langfuse: { enabled: false }   # 密钥走环境变量 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
```

Prompt 领域定制见 `config.yaml` 中被注释的 `prompt:` 段，以及 `config/research_paper.yaml`。要求条目会进入 `ChatPromptTemplate`，除 `{max_term_hints}` / `{max_terms}` 占位符外不要写花括号。

---

## 🔑 环境变量

复制 `.env.example` 为 `.env`：

```bash
SILICONFLOW_API_KEY=sk-...        # 主 API（必填）
# OPENAI_API_KEY=sk-...           # OpenAI 兼容端点（可选，与上二选一）
# GOOGLE_API_KEY=AIzaSy...        # Gemini，评测裁判使用
# LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST  # 可选
```

---

## 🐛 常见问题排查

### API 速率限制（429）

降低 `concurrency.max_concurrent_requests`（如 10 → 5）和 `rate_limit_per_minute`（如 200 → 100）。这两个上限由 `data/request_budget.db` 在同一账号的所有 worker 进程之间共享，不是每个进程各自一份。

### 部分块失败 / 需人工复核

失败块会写入占位符，不阻塞整本书。Web 任务会标记为 `partial_success`，可以在任务详情里逐块查看、编辑，或只重译失败块和指定块（其余已完成的块和人工修改都会保留）。CLI 重跑同一命令即可，成功的块走缓存。

### Worker 中断后任务卡在 processing

租约（`worker.stale_after_seconds`）过期后，worker 会自动把任务重新排队。取消或失去租约的旧执行者无法再写入状态和产物。

### 图片路径失效

后处理会自动把图片同步到任务资源目录。旧版修复工具 `scripts/fixpath.py` 仍然保留，可以手动使用。

---

## 📖 术语表系统

```json
{
  "Hyperstition": "超虚构 (Hyperstition)",
  "War Machine": "战争机器 (War Machine)"
}
```

- 使用方式：CLI 用 `-g` 指定；Web 界面和 MCP 工具都能管理术语表。也可以先用 `--suggest-glossary` 起草，审阅后再用。
- 注入方式：只把本块正文和上文中出现的术语注入 Prompt。匹配规则是英文键大小写无关的子串匹配。
- 校验方式：质检对原文中出现的术语（按长度降序，最多 `max_glossary_checks` 个）检查译文里是否出现了预期译名，判断依据是 `" ("` 前的中文部分。
- Web 任务创建时会冻结术语表快照，之后修改术语表不影响已创建的任务。

---

## 📊 性能基准

Ccru.md 实际运行（344 块，并发 10）：总耗时约 25 分钟，平均 13.8 chunks/分钟，344/344 无失败块。这是**吞吐与稳定性**数据，不代表译文质量；质量评测见 `docs/evals/`（当前提交的报告仍是 `--dry-run` 冒烟结果）。

---

## Agent skills

### Issue tracker

Issues 跟踪在 GitHub（sena1818/agentic-translator），使用 gh CLI 操作；外部 PR 不作为 triage 请求来源。See `docs/agents/issue-tracker.md`.

### Triage labels

五个 triage 标签使用规范默认名（needs-triage / needs-info / ready-for-agent / ready-for-human / wontfix）。See `docs/agents/triage-labels.md`.

### Domain docs

单上下文布局：根目录 CONTEXT.md + docs/adr/。See `docs/agents/domain.md`.
