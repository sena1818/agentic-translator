# 审校 agent

翻译完成后，质检（规则、语义提示、非文字审计）仍会标出一些问题。审校 agent 对这些问题逐条分诊：

| 结局 | 含义 | 对译文的影响 |
| --- | --- | --- |
| 已修 | 提交的局部编辑通过"逐类型不退步"检查 | 写回块译文，该问题从质检报告里移除 |
| 已驳回 | 判为误报，必须写明理由 | 译文不动；问题降为 low，消息前加"[已驳回]"和理由 |
| 待人工 | 拿不准、修改没通过检查、超出预算，或该类问题编辑修不了 | 译文不动；消息前加"[待人工]"和原因 |

默认关闭。开启：`config/config.yaml` 里 `review_agent.enabled: true`，然后在任务详情页的"审校 agent 分诊"面板点"运行分诊"
（或 `POST /api/translation/tasks/{id}/review-agent`，body `{"revision": ...}`）。只支持结构化翻译、有全书索引的任务。

## 怎么做的

- **运行框架**：每条问题是 #15 手写 agent 运行框架里的一个条目，独立上下文，只带"前面已修 / 已驳回的事项"摘要。
  每条问题的步数、token、工具调用次数都有上限（`max_steps` / `max_tokens` / `max_tool_calls`），超出即转人工。
  每一步写进 `data/review/<任务>.trace.jsonl`，可回放。
- **工具**（共享工具注册表，`src/application/tools/triage.py`，只给内部 agent，不暴露给 MCP）：
  `get_issue`、`get_context`、`glossary_lookup`、`nontext_objects`、`check_fix`，以及复用的
  `term_renderings`、`term_occurrences`、`read_segment`、`search_book`。
- **只做约束式编辑**：把段译文里恰好出现一次的一段文字换成另一段，改动片段不超过 40 字、不碰标签，一次最多 4 条。
  不能整段重写；需要重译的转人工。
- **生效条件**：提交时框架重新应用编辑、重新质检，目标问题消失且没有新增其他高/中问题才接受（与 #14 分类型修复同一标准）；
  否则把原因作为观察返回，agent 可以改了再交或转人工。重新质检与翻译时的段级质检一致：术语大小写规则跟随
  `quality.glossary_respect_case`，语义启发式只在 `quality.semantic_check.enabled` 时参与。
- **直接转人工**：缺段、截断、结构标签，以及图片 / 公式 / 代码 / 表格 / 链接 / 脚注对不上的问题，不进 agent、不花 token。
- **写回**：一次分诊结束后统一写回并发布一次。编辑在整块 Markdown 里也必须恰好出现一次才写回；
  做不到（例如被加粗标记隔开）就改为待人工，不猜位置。结果与成本另存在 `data/review/<任务>.json`。
- **对照阅读器**：待人工的问题带"[待人工]"标记出现在问题导航里；已修的问题消失；已驳回的降为 low，不再打扰。

## 为什么没有复用 `edit_chunk` 共享工具

`edit_chunk` 是"人工编辑"语义：每次调用都会标记该块为人工编辑过、记一条纠正并立即发布一次。
审校 agent 的修改是程序生效的分诊结果，一次分诊可能改很多段，所以由审校用例统一写回、只发布一次，
并在质检报告里记下每条结局（`quality_report.review`）。

## 成本与评测

见 [evals/review_agent.md](evals/review_agent.md) 与 [cost-switches.md](cost-switches.md)。
