# 各阶段的开关与额外成本

原则：主翻译流程（分块 → 翻译 → 段级质检 → 修复 → 导出）之外，凡是会额外调用模型的阶段都有配置开关，
**默认关闭或保持旧版行为**；开启后单独统计它的 token、调用次数和耗时，没有可测收益的阶段不保留。
目标是全部开启时总 token 不超过纯翻译的约 2.5 倍（尚未实测，待 #25 的前后对比）。

下表的"额外成本"注明了是实测还是按字符粗估。Cyclonopedia 整本纯翻译约 82 万 token，作为参照。

| 阶段 | Issue | 开关（`config/config.yaml`） | 默认 | 额外成本 | 依据 |
| --- | --- | --- | --- | --- | --- |
| 多路术语候选 | #16 | `multi_agent.term_ledger.sources` 加上 `keyness` / `structure`；建议同时 `max_terms: 240`、`contexts: 3` | 只抽专名（旧版行为） | 账本提示约 6k → 43k token，约占整本翻译的 0.8% → 5%（粗估） | [term_candidates.md](evals/term_candidates.md) |
| 语义质检·启发式 | #20 | `quality.semantic_check.enabled` | 关 | 不调用模型 | [qa_baseline.md](evals/qa_baseline.md) |
| 语义质检·模型 | #20 | 同上，再加 `quality.semantic_check.use_model: true`；`high_risk_only` 控制只送高风险段 | 关 | 全书全部送检约 35 万 token，只送高风险段约 31.6 万（粗估） | [methodology.md §9](evals/methodology.md) |
| 语义检索·向量 | #24 | `semantic_search` 里配置嵌入模型 | 不配置（只用关键词） | 每段原文、译文各嵌入一次，结果按文本哈希缓存；查询时一次嵌入 | [semantic-search.md](semantic-search.md) |
| 分类型修复 | #14 | 无单独开关；修复轮数由 `quality.max_fix_attempts` 控制，`quality.enable_qa_check: false` 可关掉全部修复 | 开 | 只对有问题的段调用；每轮按类型分路，假模型基准里每轮 1 → 3 次调用 | [typed_repair.md](evals/typed_repair.md) |
| 全书索引 | #13 | — | 开 | 不调用模型 | [book-index.md](book-index.md) |
| 非文字审计 | #19 | — | 开 | 不调用模型（整本约 2 秒） | [nontext_audit.md](evals/nontext_audit.md) |
| 纠正记忆的术语建议 | #22 | 填写任务的作者 / 领域才会注入 | 不填则不注入 | 每块提示多几行"参考译名" | [correction-memory.md](correction-memory.md) |
| 阅读遍 | #17 | `multi_agent.reading_pass.enabled` | 关 | 每章一次调用，全书原文多送一遍：Cyclonopedia 39 次调用、约 19–28 万 token（纯翻译的 23–34%，粗估，上下界分别对应笔记为空 / 写满）；画像替换首中尾采样，随每块发送的部分上限 1200 字符，与原画像（约 1k 字符）相当 | [reading_pass.md](evals/reading_pass.md) |
| 审校 agent | #21 | `review_agent.enabled`（开启后在任务详情页手动运行）；`max_issues`、每条问题的 `max_steps` / `max_tokens` / `max_tool_calls` | 关 | 每条进 agent 的问题约 3–4k token（假模型按字符估），单条上限 12k；Cyclonopedia 基线有 53 条会进 agent（另 3 条直接转人工），约 20 万 token，上限约 64 万（粗估，纯翻译约 82 万） | [review_agent.md](evals/review_agent.md) |
| 术语 agent | #18 | `multi_agent.term_agent.enabled`；`max_items`、`max_steps`、`max_tokens`、`max_tool_calls` 控制上限 | 关 | 每条有歧义的候选最多 `max_steps` 次模型调用、`max_tokens` token（默认 8 次 / 1.6 万）；最坏情况 = `max_items × max_tokens`（默认 64 万）。实际用量未用真实模型测过；运行后写入任务质检摘要 `term_agent_tokens` | [term_agent.md](evals/term_agent.md) |

## 开启全部可选阶段

在所用配置文件里改这几项：

```yaml
multi_agent:
  term_ledger:
    sources: [proper, keyness, structure]
    max_terms: 240
    contexts: 3
    batch_size: 60
  reading_pass:
    enabled: true
quality:
  semantic_check:
    enabled: true
    use_model: true
review_agent:
  enabled: true
```

前后对比时，"旧版"与"新版·默认"、"新版·全开"三组分别跑，才能分开说明默认配置和可选阶段各自带来的变化与代价。
