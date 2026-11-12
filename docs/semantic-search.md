# 语义检索：按意思找原话

读者用一句大概的话（中文或英文）找到书里意思相近的段落，点击跳到对照阅读的位置（PRD #10，issue #24）。
它建在[全书索引](book-index.md)之上，只服务查找，不改动译文。

## 入口

| 入口 | 说明 |
| --- | --- |
| Web 审校页 | 任务详情页的"按意思找原话"搜索框（结构化翻译完成后出现）。结果显示原文与译文，"在对照阅读中定位"跳到 `#/tasks/<id>/read?seg=N` |
| HTTP | `GET /api/translation/tasks/{id}/index/semantic?q=&limit=` |
| MCP | 只读工具 `search_book_by_meaning(task_id, query, limit)` |

单文件离线阅读器（导出的 HTML）不包含语义检索。

## 怎么排序

1. **关键词排序（BM25）**：每段的原文和译文合成一篇文档。英文按词（小写、去常见虚词、粗略去词尾），中文按相邻两字。
   与全书索引的"包含全部关键词"检索不同，它按重叠程度打分：改写过的句子只要有部分词重叠就能排上来。
2. **向量**：每段原文、译文各算一条向量，取与查询相似度较高的一条。
3. **融合**：两路各取前 100 名，按倒数排名融合（RRF，k=60）。

**跨语言**：一段同时带原文和译文，中文查询命中译文、英文查询命中原文都算命中这一段。所以"只在英文原文里出现的说法"
用中文也能找到（经由这段的译文），反之亦然；有多语言向量模型时，中文查询还能直接和英文原文比相似度。

**退回仅关键词**（结果里 `mode: "lexical"`，`hint` 写明原因，界面显示提示）：
- 未配置向量模型（默认）；
- 向量模型只支持英文而查询是中文（或相反）；
- 向量模型调用失败。

## 配置

`config/config.yaml`：

```yaml
semantic_search:
  embedder: none        # none | openai | local
  model: null           # openai 例：BAAI/bge-m3；local 例：BAAI/bge-small-en-v1.5
  base_url: null        # openai 兼容服务地址
  api_key_env: null     # 密钥所在的环境变量
  languages: null       # 默认 openai 为 [en, zh]，local 为 [en]
```

- `openai`：任何 OpenAI 兼容的 `/v1/embeddings` 服务。多语言模型（如 SiliconFlow 上的 BAAI/bge-m3）可以同时给原文、译文和中英文查询算向量。
- `local`：本机 transformers 模型，只从本地 Hugging Face 缓存加载，不会联网下载；需要可选依赖 `pip install -e ".[semantic-local]"`。

## 向量缓存

- 每本书一个 `data/index/<task_id>.vectors.sqlite`，每段原文、译文各一条，附文本哈希。
- 翻译完成（导出、写完全书索引）后生成；检索前再按哈希补齐，人工编辑过的段只重算这几段。
- 换嵌入模型时整表作废重算；删除任务时一并删除。

代码：排序在 `src/domain/index/semantic_search.py`（纯函数），嵌入器在 `src/infrastructure/embeddings/`，
缓存在 `src/infrastructure/persistence/vector_store.py`，用例在 `src/application/use_cases/semantic_search.py`。

## 效果

2026-11-12，Cyclonopedia 2026-11-11 基线译文（1038 段），36 条"读者凭印象写的一句话 → 应命中的段"用例
（`docs/evals/datasets/semantic_search_cyclonopedia.json`，自撰改写，不含书籍原文）。命中 = 应命中段出现在前 5 条。

- **keyword**：全书索引原有的关键词检索（段内包含全部关键词）；
- **lexical**：本功能未配置向量模型时的退回方式（BM25）；
- **hybrid**：BM25 + 本机 `BAAI/bge-small-en-v1.5`（只支持英文，所以中文查询仍走 lexical）。

| 用例 | 数量 | keyword | lexical | hybrid |
| --- | ---: | ---: | ---: | ---: |
| 英文·改写 | 12 | 0（0%） | 10（83%） | 11（92%） |
| 中文·改写 | 12 | 0（0%） | 12（100%） | 12（100%） |
| 英文·少重叠（同义替换、概括） | 8 | 0（0%） | 4（50%） | 6（75%） |
| 中文·少重叠 | 4 | 0（0%） | 2（50%） | 2（50%） |
| **合计** | 36 | 0（0%） | 28（78%） | 31（86%） |

平均倒数排名（MRR）：lexical 0.736，hybrid 0.775。

读数：
- "包含全部关键词"的检索对自然语言句子几乎没用：一句话里总有几个词不在同一段。
- BM25 已能找回大部分改写句；中文查询靠段的译文命中，"只在英文原文里出现的说法"（如 war machine、devil's excrement）
  用中文能找到，验证了跨语言这一条。
- 向量主要补"少重叠"的英文查询（50% → 75%）。中文少重叠查询没有提升，因为本机模型只支持英文；
  配置多语言模型（如 bge-m3）后预期会改善，但本次没有可用的多语言嵌入服务，**未实测**。
- 用例由实现者自己编写，数量少，且"改写"组与原文用词重叠较多，数字偏乐观；"少重叠"组更接近真实使用。
- 本机 CPU 上 bge-small 给全书 1038 段算向量约 4~5 分钟，之后检索只需算查询向量；向量缓存后不再重算。

复现：

```bash
python scripts/eval_semantic_search.py <源文件.md> <X.records.json> \
  --config config/tokenrhythm.yaml --cases docs/evals/datasets/semantic_search_cyclonopedia.json \
  --embedder local:BAAI/bge-small-en-v1.5 --workdir <缓存目录>
```
