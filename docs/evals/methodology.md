# 翻译质量评测方法论

本目录是 issue #6 的评测证据链：**可观测 → 对照评测 → RAG 门槛决策**。
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
- **打分维度**（1~5 Likert）：准确性 accuracy、流畅度 fluency、术语一致性 terminology。
- **综合分**：三维平均；但准确性 ≤ 2（严重误译）时综合分不超过准确性分，避免通顺的错译被流畅度拉回中等。
- **裁判 Prompt** 全文在 `judge_prompt.md`（代码直接读取，占位符 `{source}/{translation}/{glossary}`），可查可改。
- 打分解析只对格式宽容（```json 代码块、前后解释文字）。缺维度、分值不是数字、超出 1~5、或找不到 JSON 时，
  该条记为**无效裁决**：不参与均值，在分数表"无效裁决"列单独计数（见 `src/evaluation/judge.py`）。
  旧做法把缺失维度补成 3 分，会把"没评出来"当成中等表现。
- 裁判仍是自动辅助，异源只能减轻自我偏好，结论需要人工校准。

## 3. 两组对照

每组只在"生产基线"（多 Agent 开 + 术语表有）上拨动一个开关。原先还有一组编排引擎对照（LangGraph vs Native），已随 native 引擎删除而移除：

| 对照组 | 变体 A | 变体 B | 拨动的开关 |
| --- | --- | --- | --- |
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

native 引擎已在 #25 删除，依据是两引擎等价性测试长期逐字节一致，详见 ADR-0001 的"落地情况"一节。
原等价性测试改为单引擎契约测试 `tests/test_engine_contract.py`。

## 7. RAG 门槛决策

见 `../adr/0002-rag-translation-memory-threshold.md`：统计术语表外重复短语的译法一致率，
没有候选短语时报告 `rate=null`、`insufficient-data`，不作架构结论。存在候选时，≥ 90% 写 ADR 记录不做，< 90% 另立 sqlite-vec 翻译记忆的 PRD。一致率算法见 `src/evaluation/consistency.py`。

**按句对齐**：块级 (原文, 译文) 先按空行拆段，再按句末标点拆句；两侧段数、句数都相同才一一对齐，
短语的译法签名只在它所在的那一句的译文里求。对不齐的段落不参与统计，报告里单独给出数量。
旧做法在整块译文上求公共子串，两块里碰巧有同一句无关的话就会造成假一致。
这仍是启发式指标（同一句里另一个重复短语的译法也可能成为签名），不能单独作为架构决策依据。

## 8. 注入错误基准（质检的召回率与误报率）

不需要密钥、不需要人工标注：取正确的段对，用程序把译文改坏成七类已知错误（反转否定、改变情态强弱、
交换数字、漏译半句、换错术语、改链接地址、改代码内容），统计质检报出新增问题的比例；
原样段对上报出任何问题算误报。

```bash
translator-qa-bench --out docs/evals/results                       # 自带段对 + 六个种子反例
translator-qa-bench --records <run>/<书名>.records.json --glossary <术语表>.json   # 另在本地真实译文上跑
```

- 自带段对在 `datasets/injection_pairs.json`（自撰，不含版权文本）。种子反例来自 `translation-quality-deepening.md`。
- 当前规则质检的基线数字见 [qa_baseline.md](qa_baseline.md)；之后改质检（#14、#19、#20）都以这里的召回率和误报率为验收。
- 注入错误比真实错误规整，召回率是"能否抓住这类已知错误"，不是真实译文的检出率。
- 每次同时跑 `rules`（规则质检）和 `rules+semantic`（规则 + 语义质检启发式），并给出模型语义检查的
  token 粗估与"只查高风险段"时的召回上限；`--semantic-model` 另调用模型（需要密钥）。
- 加 `--with-audit` 时另跑 `rules+audit` 与 `rules+semantic+audit` 两列（全书非文字审计，需要 pandoc）。

## 9. 语义质检（否定、情态、数字角色、漏译）

配置在 `config/config.yaml` 的 `quality.semantic_check`，默认关闭，关闭时流程与未接入时一致。

- **启发式**（`src/domain/rules/semantic_signals.py`）：全量段落都跑，不花 token。
- **模型检查**（`use_model: true`，`src/pipelines/translate/semantic_checker.py`）：按批送模型，结构化输出
  原文片段、译文片段、类型、严重度、理由，允许"无法判定"；`high_risk_only` 只送含否定/情态/数字/长句的段；
  `model`/`base_url`/`api_key_env` 可指定与翻译员异源的模型，留空则复用审校模型。
- 在段级修复之后运行，结论以 `kind="semantic"` 的问题写入质检报告，消息前缀"语义提示·规则/模型"；
  **只报告、不触发自动修复，也不决定块是否通过**——它是提示，不是正确性保证，交给人工或审校 agent（#21）分诊。
- 本阶段的 token、调用次数和耗时单独累计在 `TranslationEngine.semantic_checker.usage`。
- 已缓存的块不会补跑语义检查；对已有译文开启时需清缓存或重译相关块。

## 10. 修复基准（分类型修复是否"修好一类、新增另一类"）

```bash
translator-repair-bench --max-fix-attempts 1
```

用注入基准的段对和脚本化的假模型（翻译员注入错误、审校员偶尔手滑引入新问题），统计被采用的手滑修复、
修复后仍有问题的段和模型调用次数。改动前后对比见 [typed_repair.md](typed_repair.md)。

## 11. 全书非文字审计

导出时对整本书运行的确定性审计（图片、公式、代码、表格、脚注尾注、链接、编号引用、图注），区分翻译引入与原文本身的问题，
不调用模型。规则、注入检出和 Cyclonopedia 全书结果见 [nontext_audit.md](nontext_audit.md)；对已完成的翻译重跑：
`python scripts/audit_structured.py <源文件.md> <X.records.json> --config <翻译时的配置> --assets <图片目录>`。

## 12. 术语候选基准（藏起术语表条目后的召回）

```bash
translator-term-bench --source <书>.md --glossary data/glossaries/CPglossary.json --out docs/evals/results
```

把人工维护的术语表按条目轮流分成两折，每折藏起一半、另一半作为已有术语表，看候选能找回多少被藏起的条目；
召回只在"书里至少出现 2 次"的条目上算。同时报告候选数量、各来源命中、歧义标记、已记录误判（专名被拆成碎词）
是否回退，以及账本提示的 token 粗估。不调用模型。结果与读数见 [term_candidates.md](term_candidates.md)。

## 13. 审校 agent 分诊基准（注入错误 + 已知误报）

```bash
translator-review-bench --out docs/evals/results          # 不需要密钥：现状 vs 脚本化假审校员
translator-review-bench --model config                    # 用配置里的审校模型（需要密钥）
```

混合集由 #11 的注入错误（质检能报出的那部分）和 `datasets/review_false_positives.json` 的已知误报组成；
统计正确修复（修后与原样译文一致）、正确驳回、转交人工、误报被改、真错被驳回的比例，核对每条生效的修改
都通过了逐类型不退步检查，并给出每条问题的步数与 token。脚本化假审校员知道标准答案、按固定节奏犯错，
只检验流程与闸门；真实模型的分诊能力要用 `--model config` 测。结果与读数见 [review_agent.md](review_agent.md)。
