# 术语 agent（#18）：设计与评测

日期：2026-11-12。**本页没有真实模型的数字**：需要付费密钥，留到 #25 与前后对比一起跑。下面的流程、
评测条目和测试都已就绪，补跑只需一条命令。

## 做什么

一次性的术语账本调用只凭几处上下文就给全书定一个译名。遇到"既是普通词又是专名"的词容易定错：
Ccru 全书实测里 Mission 被定成"使命"（实为海盗 Captain Mission 的名字），Gates 被定成"盖茨"
（实为数字图里的 Gates），而且这个错译会被术语一致性检查强制到全书每一处。

术语 agent 只接手被确定性信号（#16）标为有歧义的候选，在手写运行框架（#15，ADR-0005）上逐条查证：

| 工具 | 作用 |
| --- | --- |
| `find_occurrences` | 全书出现处，每页 6 段，带段号、章节、原文片段与已有译文 |
| `rendering_distribution` | 已有译文里的译法分布（翻译前运行时通常还没有译文，会明说） |
| `lookup_glossary` | 用户术语表（必须遵守）、本书账本、同作者 / 同领域其他书的参考译名 |
| `search_segments` | 按关键词找段，用来找全名或搭配（如 "Captain Mission"） |
| `search_by_meaning` | 按意思找段（可选，传入语义检索时才注册） |

这些工具用共享工具注册表（#23）的 `@shared_tool` 写在 `src/application/tools/term_agent.py`，由
`build_tool_suite(ToolContext(extras=...), modules=[term_agent])` 组装后交给运行框架。它们读的是翻译前在内存里建的
全书索引（放在 `ctx.extras`），不收 task_id，所以**不加进 DEFAULT_MODULES、不暴露给 MCP**：
MCP 面向已翻译的任务，已经有 `term_occurrences`、`term_renderings`、`read_term_ledger`、`search_book` 这些等价工具。

结论只有三种：`term`（一个或多个义项，各带译名、词义、适用范围、证据段）、`not_term`（普通词）、
转交人工。提交时框架核对：引用的段号必须出现在这一条实际查到的工具输出里，否则把原因退回给模型重做。

并入规则：

- 只有**单一译名**的 `term` 并入本次术语表；多义项（人名与普通词并存）不强制全书统一，只记录；
- 已在用户术语表或账本里的词不运行 agent，也不会被覆盖；
- 转交人工的条目带抽取时的上下文，出现在 Web 术语表页的"术语 agent 转交待审"里；
- 每条决定写入 `data/term_agent/<任务>.json`（含证据、步数、token），单一译名的写进纠正记忆的术语条目
  （来源"术语 agent"，带义项与证据段），人工条目不会被覆盖。

## 开关与成本

`config/config.yaml` 的 `multi_agent.term_agent`，**默认关闭**；关闭时账本缓存键、候选和调用都与旧版相同。
开启后有歧义的候选不再进一次性账本调用（其余候选照常）。每条候选的上限：8 步（每步一次模型调用）、
1.6 万 token、6 次工具调用；每本书最多查证 40 条。用量写进任务质检摘要（`term_agent_decided`、
`term_agent_handoffs`、`term_agent_tokens`），并随账本缓存保存，续跑不重复花费。

歧义候选主要来自多路候选（`sources` 加 `keyness` / `structure`）；只抽专名时候选少、歧义也少。

## 评测：一次性调用 vs agent

```bash
translator-term-agent-bench --source <Cyclonopedia>.md --glossary data/glossaries/CPglossary.json --config <配置>.yaml --out docs/evals/results
translator-term-agent-bench --source data/temp/ccru/Ccru.md --book Ccru --config <配置>.yaml
```

评测条目 = 有歧义、且有答案可对照的候选：

- 被藏起的术语表条目（与 `translator-term-bench` 同样分两折，每折藏一半），答案是该条目的译名；
- 已记录的误判（`datasets/term_misjudgments.json`），只知道错的写法。

指标：误判率（强制译名与答案不符，或落在已知错误里）、转交人工比例、平均步数、token。agent 的多义项、
普通词、转交都不强制，不算误判，所以必须同时看转交比例，不能把"全部转交"当成零误判。

`--dry-run` 不调用模型，列出的评测条目：

| 书 | 条目 | 来源 |
| --- | --- | --- |
| Cyclonopedia | Earth、Sun、Oil、Tellurian、Dust、Hidden Writing、petropolitics、Horde、Hamid Parsani、Petroleum（10 条） | 藏起的 CPglossary 条目 |
| Ccru | Mission（≠ 使命）、Gates（≠ 盖茨） | 已记录误判 |

Centauri（≠ 半人马）没有被歧义信号标出，agent 不会接手它；这是 #16 歧义规则的已知漏判。

| 方法 | 误判率 | 转交人工 | 平均步数 | token |
| --- | ---: | ---: | ---: | ---: |
| 一次性调用 | 未测 | — | 1 | 未测 |
| 术语 agent | 未测 | 未测 | 未测 | 未测 |

## 测试

`tests/test_term_agent.py` 用脚本化假模型覆盖：查证后提交（拆分义项、证据段可追溯）、引用没查到的段被拒绝后改正、
转交人工（保留上下文进入待审清单）、预算用尽降级、术语表已有的词不运行、关闭时与现状一致、开启时只接手有歧义的候选
且结果随缓存恢复；以及基准的计分（一次性调用定成"使命"记误判，agent 拆出人名不记）。
