# 多路术语候选：召回、误判与成本

日期：2026-11-12。对应 issue #16。不调用模型，数字可以一条命令复现：

```bash
translator-term-bench --source <Cyclonopedia 源文>.md --glossary data/glossaries/CPglossary.json --out docs/evals/results
```

书籍原文不入库，这里只记数字和词条名。完整输出：[results/term_candidates.md](results/term_candidates.md)。

## 改了什么

术语账本原先只抽大写专名（按次数取前 120 条），每个候选只带首次出现处一段上下文。现在候选来自三路：

| 来源 | 抓什么 | 怎么排 |
| --- | --- | --- |
| 专名 proper | 大写专名与缩写，不拆专名短语 | 与旧做法同一列表、同一顺序（前 120 条） |
| 关键度 keyness | 本书频率显著高于通用英语的词和短语（本书 zipf − wordfreq zipf），抓小写自造概念词 | 次数与超过门槛的余量，专名之外最多补 80 条 |
| 结构 structure | Document IR 里被强调、加引号的词，标题里的词；正文中至少出现 2 次 | 正文次数，前两路之外最多补 40 条 |

- 同一个词被多路命中时合并来源，不重复占配额；总上限 240 条。
- 每个候选从全书索引取 3 处差异较大的上下文：首次出现、带定义意味的、不同章节的；词重合度高的片段不重复取。
- 歧义信号（确定性）：大小写混用（两种写法各 ≥ 2 次且少数方 ≥ 10%）、可能只是句首大写、同时是常用英语词
  （zipf ≥ 4）、全书索引里已有两种以上译法。有歧义的多给一处上下文。
  `multi_agent.term_ledger.defer_ambiguous: true` 时它们不进账本，留给术语 agent（#18）。
- 账本按 60 条一批调用模型，后一批能看到前面批次已定的译名。

## 结果：Cyclonopedia，藏起 CPglossary 条目

术语表 72 条，其中在这本书的转换稿里出现 ≥ 2 次的 53 条（Blobject、Katechon、Aquatic 等 19 条一次都没出现，
任何抽取都找不回）。两折轮流藏起，每条都被藏过一次。

| 方法 | 每折候选 | 找回 | 召回（出现 ≥ 2 次的 53 条） |
| --- | ---: | ---: | ---: |
| 现状：只抽大写专名 | 120 | 18 | 34% |
| 多路候选 | 240 | 29 | **55%** |

- 新找回 11 条：Dustism、Fog of War、Hole complex、Horde、Monotheism、Petropolitics、Plot Hole、Polytics、
  Radical Openness、Reza Negarestani、War Machine。
- 现状能找到、多路丢失的：**无**（专名一路保持原列表）。
- 仍找不回的主要是两类：
  - 常用词在书里以小写出现：Solid、Fluid、Swarm、Vortex、Spiral、Paranoia、Schizophrenia。关键度低，也很少被强调；
  - 次数少、排在配额之外的：Cyclonopedia（6 次）、Incognitum Hactenus（3）、Tellurian Insider（4）。
    把关键度配额提到 100 能多找回 War on Terror 和 Incognitum Hactenus。

## 小写概念词（验收实例）

关键度一路抽到的小写候选（括号内为次数），旧做法一个都抽不到：
polytical（32）、occultural（32）、petropolitical（23）、ungrounding（17）、trison-cell（16）、schizotrategy（14）、
poromechanics（13）、unlife of war（12）、holey space（18）、urbanized war（26）、affordance（30）、cross of akht（34）。

其中也有 OCR 与句子碎片带来的噪声（parsani defacing、persia parsani、defacing the ancient），由账本调用判 keep=false。

## 已记录误判：无回退

- 专名被拆成碎词（York、Track、Old、New、Sunken，structured_gemini.md 记录）：多路候选里一个都没有；
  `tests/test_term_sources.py` 和 `tests/test_document_ir.py` 有对应用例。
- Mission、Gates（Ccru 全书实测中账本误判过的词，见 structured_gemini.md）：在 Ccru 上两者都被标为
  "大小写混用 + 同时是常用英语词"，并带 4 处上下文，其中同时出现普通词义（a special mission、the gates of …）
  与人名（Captain Mission、Bill Gates），模型不再只凭一处上下文判断。
- Centauri（误译"半人马"）没有歧义信号：它在书里几乎总以 Alpha Centauri 出现，规则判断不出译法争议，
  这类问题要等 #22 纠正记忆或人工审校。

## 候选数量与成本

| | 现状 | 多路 |
| --- | ---: | ---: |
| 候选（整份术语表都给出） | 120 | 240（专名 120、关键度 126、结构 58，可重叠） |
| 带歧义标记 | — | 93（大小写混用 59、常用词 53、句首大写 6） |
| 每个候选的上下文 | 1 | 平均 3.3 |
| 账本调用次数 | 1 | 4 |
| 账本提示 token（粗估） | ≈ 6.2k | ≈ 42.6k |

同一本书的整本翻译约 82 万 token（2026-11-11 基线），账本从约 0.8% 涨到约 5%，是每本书一次的固定成本。
账本结果按源文、术语表和配置缓存，断点续跑不重复花费。想省可以调低 `max_keyness` / `max_structure`，
或用 `sources` 关掉某一路；`contexts: 3` 已是 issue 要求的下限。

## 局限

- 召回只说明候选里有没有这个词，不代表模型最终给出的译名对；最终译名的质量要等 #25 用真实模型评。
- 关键度依赖 wordfreq；未安装时这一路自动关闭，只剩专名和结构两路。
- 没有 Document IR（非结构化模式）时按空行拆段，结构线索在 pandoc 可用时解析一次全文得到。
