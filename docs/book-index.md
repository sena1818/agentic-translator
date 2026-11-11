# 全书索引

以段为单位保存全书的原文、译文和位置，提供三种查询。它是术语 agent、审校 agent、纠正传播、
一致率指标和 MCP 查询的共同底座（PRD #10，issue #13）。

## 存什么

每个任务一个 SQLite 文件：`data/index/<task_id>.sqlite`。每行一段：

| 字段 | 含义 |
| --- | --- |
| `position` | 全书顺序号，覆盖所有含文字的位置（包括参考文献等保留原文、不送模型的段） |
| `seg_id` | 对照阅读器的段号，只有送去翻译的段才有 |
| `chunk_index` | 所在块 |
| `section_path` | 章节路径 |
| `in_note` / `verbatim` | 是否在脚注里 / 是否保留原文 |
| `status` | `source_only`（还没译）、`translated`、`unaligned`（块译文结构对不上原文）、`failed` |
| `source` / `translation` | 纯文本（不含代码、公式、图片地址） |

`meta.source_fingerprint` 记录源文与分块参数的指纹，变化时整表重建。

## 什么时候更新

只在结构化翻译模式（`document_ir.enabled` 且装了 pandoc）下建立：

1. 规划完分块、开始翻译前：建只含原文的索引，此时就能查出现处；
2. 每译完（或失败）一块：写入该块各段译文；
3. 导出后：以最终记录为准再写一遍（包括缓存命中、没有触发进度事件的块）；
4. 人工编辑一块后：重写该块；
5. 删除任务时删除索引。

写索引失败只记日志，不影响翻译和导出。

## 查询

| 查询 | HTTP | 说明 |
| --- | --- | --- |
| 关键词检索 | `GET /api/translation/tasks/{id}/index/search?q=&field=any\|source\|translation` | 包含全部关键词（空白分隔、不区分大小写）的段，分页 |
| 出现处 | `GET /api/translation/tasks/{id}/index/occurrences?term=&case_sensitive=&variants=` | 全部出现段、命中位置与实际写法、前后段原文、该段译文；按段分页，`total_matches` 是全书命中次数 |
| 译法分布 | `GET /api/translation/tasks/{id}/index/renderings?term=` | 各段对应的中文译法及次数，每种附证据段号 |

词形：短语内空白与连字符可互换（time war / time-war）；`variants=true`（默认）时最后一个词允许复数、
所有格、-ed/-ing（city → cities、mission → missions）；前后不能紧挨其他字母，"war" 不命中 "warp"。

代码入口：查询逻辑在 `src/domain/index/book_index.py`（纯函数），抽段在 `src/pipelines/structure/indexing.py`，
存储在 `src/infrastructure/persistence/book_index_store.py`，用例在 `src/application/use_cases/book_index.py`。

## 译法分布怎么算

不调用模型，每段按三条证据定译法：

1. 已知译法（术语表里该词的译名）出现在该段译文里；
2. 从含该词的段的译文中挖出的候选：2~6 字中文片段，在这些段里的覆盖比例减去在其余段里的覆盖比例
   （区分度）不低于 0.3；紧挨"（原词）"括注的片段加分；互相包含的候选（"虚构"与"超虚构"）只留更专用的一个；
3. 都没有时，原词原样留在译文里记为"保留原文"，否则记为"未识别"。

这是启发式统计，结果附证据段号供人工或术语 agent 核查。定不了的宁可记"未识别"，不拿上下文里的无关词充数。

## 验证

- Ccru（3072 段，只有原文）：20 个词（Lemuria、Numogram、AxSys、the……）的出现处命中次数与对 pandoc 纯文本
  输出做同样正则搜索的次数全部一致。
- Cyclonopedia 2026-11-11 基线译文（1038 段）：Hyperstition → 超虚构 13 / 保留原文 1；war machine → 战争机器 18；
  Numogram → 数图 7；oil → 石油 82 / 未识别 3。少数段会被归到上下文里的高频片段（如 Mission 的 1 段归到"为一"），
  这类低次数条目需要看证据段再判断。
