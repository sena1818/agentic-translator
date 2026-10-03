# 翻译质量评测方法论

本目录是 issue #6 的评测证据链：**可观测 → 三组对照评测 → RAG 门槛决策**。
目标是用可复现的数据回答两个问题：多 Agent / LangGraph 这些重构到底有没有带来质量增益？
是否值得为"术语表外重复短语"另立项做 RAG 翻译记忆？

## 1. 数据集

`datasets/` 下两篇代表性英文摘选（均为规避版权自撰，覆盖学术/技术两种文体）：

| 样本 | 文体 | 术语表 | 说明 |
| --- | --- | --- | --- |
| `philosophy-01` | 后现代哲学 | `data/glossaries/CPglossary.json` | Cyclonopedia 风格：石油政治、超虚构、战争机器 |
| `paper-01` | ML 论文 | `data/glossaries/research_paper_glossary.json` | 过程监督 / 奖励模型 / 思维链 |

数据集清单在 `datasets/dataset.json`，路径相对该文件。可自行增删样本，格式不变。

## 2. 裁判（LLM-as-judge）

- **裁判模型：Gemini**，与选手 DeepSeek **异源**，避免同源模型的自我偏好。
- **打分维度**（1~5 Likert）：准确性 accuracy、流畅度 fluency、术语一致性 terminology；综合分取三维平均。
- **裁判 Prompt** 全文在 `judge_prompt.md`（代码直接读取，占位符 `{source}/{translation}/{glossary}`），可查可改。
- 打分解析对 ```json 代码块、前后解释文字、越界分值、缺失维度都做了容错（见 `src/evaluation/judge.py`）。

## 3. 三组对照

每组只在"生产基线"（`langgraph` + 多 Agent 开 + 术语表有）上拨动一个开关：

| 对照组 | 变体 A | 变体 B | 拨动的开关 |
| --- | --- | --- | --- |
| 编排引擎 | LangGraph | Native | `TranslationEngine(engine=...)` |
| 多 Agent 协作 | 开（分析员+审校员） | 关（单翻译员） | `multi_agent.enabled` + `quality.enable_qa_check` |
| 术语表 | 有 | 无 | 传入 `glossary` 或 `{}` |

每个变体对每条样本各跑一次真实翻译，交裁判打分，按样本对齐后取均值。

## 4. token / 延迟 / 成本

- **token**：`UsageMetadataCollector`（一个 LangChain 回调）在翻译与裁判两侧分别累计每次 LLM 调用的
  token 用量，无需 Langfuse 服务也能拿到数据；接入 Langfuse 时同一批调用链亦可在 Langfuse UI 查看。
- **延迟**：runner 以 wall-clock 计每条样本的翻译耗时，取均值。
- **成本**：报告汇总总 token 与调用次数；折算人民币需按当时选手/裁判单价填入 `--estimated-cost` 或报告的成本节。

## 5. 复现

```bash
# 真实评测（需要密钥：SILICONFLOW_API_KEY 选手 + GOOGLE_API_KEY 裁判）
translator-eval --out docs/evals/results

# 冒烟：假模型 + 假裁判机械跑通同一条流水线（无需密钥，用于验证脚本不腐化）
translator-eval --dry-run --out docs/evals/results
```

产物写入 `--out`：`report.md`（人读）+ `report.json`（原始数据）。
可选参数：`--judge-model`（默认 `gemini-2.0-flash`）、`--threshold`（RAG 门槛，默认 0.90）、`--dataset`、`--prompt`。

评测框架的纯逻辑（打分解析、聚合、一致率）由 `tests/test_evaluation.py` 用假裁判 Runnable 单元验证，
不触网、不需密钥。

## 6. 引擎去留结论

LangGraph 与 Native 两引擎共用同一批组件（prompt_builder / translation_client / quality_pipeline），
`tests/test_engine_equivalence.py` 已证明二者对同一输入逐字节等价。本评测的引擎对照进一步用真实模型
确认质量/延迟/token 无显著差异后，即满足 ADR-0001 删除 native 的前置条件（删除动作留待收尾 issue）。

## 7. RAG 门槛决策

见 `../adr/0002-rag-translation-memory-threshold.md`：统计术语表外重复短语的译法一致率，
没有候选短语时报告 `rate=null`、`insufficient-data`，不作架构结论。存在候选时，≥ 90% 写 ADR 记录不做，< 90% 另立 sqlite-vec 翻译记忆的 PRD。一致率算法见 `src/evaluation/consistency.py`。
