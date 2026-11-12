# 纠正记忆：书内传播与按范围复用术语

把人工修改变成系统下次还能用的记忆（issue #22）。只做两件事：本书内把一次译名纠正传播到全书；
同作者、同领域的下一本书把这些译名当作**建议**。不做通用的跨书记忆。

## 流程

```
人工编辑一块 ──► 纠正记录（改了什么、在哪、何时）
      │
      └─ 改掉的是某术语的译名？──► 提示"全书改译名"
                                        │
               全书索引找出现处 ◄────────┘
                    │
              预览（逐段：原文 / 旧译文 → 新译文；不改的段及原因）
                    │ 勾选
              批量应用 ──► 只替换选中段里的旧译名 ──► 改动的块重新质检 ──► 发布新产物
                    │
                    └─► 术语条目（来源=人工，范围=本书/同作者/同领域）+ 覆盖本书账本
                                                │
                     同作者 / 同领域的下一本书 ◄─┘  翻译提示里的"参考译名"（标明来源书），不进术语校验
```

## 设计要点

- **只改该改的地方**：传播只看原文含该术语的段；其他段里碰巧出现同样两个字不动。
  旧译名作为更长译名的一部分出现时也不动（"虚构"之于"超虚构"，以及新译名本身包含旧译名的情况），
  所以重复应用是幂等的。
- **写回块译文的两条路**（`src/pipelines/structure/propagation.py`）：
  - 该块里所有可替换处都落在选中的段里：直接在 Markdown 字符串上替换，其余字符一个不动；
  - 否则解析成 AST，只改选中段的文字节点，再用 pandoc 写回。
- **质检按新译名**：人工决定写入本书术语条目，并覆盖本书术语账本；审校、传播后的质检与之后的重译都用
  "任务术语表 + 账本 + 本书人工纠正"，因此改动的块按新译名检查。
- **建议不是约束**：跨书条目只拼进结构化翻译提示的"参考译名"一节（只列本块出现的术语），
  不进入术语表，`TranslationValidator` 看不到它们，也就不会报术语不一致。
- **两种状态分开**：保存译文记为"人工编辑过"（`quality_report.edited`），"已审校"需要单独确认
  （`POST .../chunks/{index}/review`）。

## 存储

`data/memory/memory.sqlite`，所有书共用：

| 表 | 内容 |
| --- | --- |
| `corrections` | 每次编辑 / 传播一条：任务、书名、块号、类型、术语、旧/新片段、段位置、时间 |
| `term_entries` | 每本书每个术语一条：译名、义项、范围、来源、作者、领域、证据段；人工条目不被自动来源覆盖 |

一本书译完时，它实际使用的术语表与账本译名会记成条目（范围由作者/领域决定），供同作者、同领域的下一本书参考。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/translation/tasks`（表单 `author`、`domain`） | 创建任务时填写作者、领域，可留空 |
| PUT | `/api/translation/tasks/{id}/chunks/{index}` | 保存编辑；返回 `term_changes`（识别出的译名纠正） |
| POST | `/api/translation/tasks/{id}/chunks/{index}/review` | 标记已审校 |
| POST | `/api/translation/tasks/{id}/corrections/preview` | `{term, old, new}` → 逐段预览与不改的段 |
| POST | `/api/translation/tasks/{id}/corrections/apply` | `{term, old, new, positions?, revision, scope?}` → 批量应用 |
| GET | `/api/translation/tasks/{id}/corrections` | 本书纠正记录 |
| GET | `/api/translation/tasks/{id}/term-suggestions` | 本书能看到的跨书建议及来源 |

用例在 `src/application/use_cases/correction_memory.py`，Web 路由与之后的 MCP 工具（#23）共用。

## 限制

- 只在结构化翻译模式（Document IR + pandoc）下可用：传播依赖全书索引的段位置。
- 块结构与原文对不上的段没有段级译文，预览里列为"需手动处理"，不自动改。
- 旧译名跨越格式边界（一半加粗）时，AST 路径找不到它，该处不改。
- 参考译名只注入结构化翻译路径，旧的 Markdown 分块路径不注入。
