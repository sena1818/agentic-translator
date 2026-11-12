"""语义检索评测：一组"改写后的句子 → 应命中的段"，报告前 5 条命中率，对比三种检索。

- keyword：全书索引原有的关键词检索（段内包含全部关键词，按全书顺序）；
- lexical：语义检索未配置向量模型时的退回方式（段级 BM25，原文 + 译文）；
- hybrid：BM25 + 向量（RRF 融合），需要 --embedder。

用法:
    python scripts/eval_semantic_search.py <源文件.md> <X.records.json> --cases <cases.json> \\
        [--config config/tokenrhythm.yaml] [--embedder local:BAAI/bge-small-en-v1.5] [--report <输出.md>]

cases.json：{"cases": [{"query": "...", "expect": [段 position, ...], "note": "..."}]}。
索引建在临时目录，不写入 data/。--dump N 打印 N 个已译段，供编写用例时参考。
"""

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.getcwd())

from src.application.use_cases.book_index import BookIndexService  # noqa: E402
from src.application.use_cases.semantic_search import SemanticSearchService  # noqa: E402
from src.domain.index.semantic_search import query_language  # noqa: E402
from src.infrastructure.embeddings import EmbedderSetup  # noqa: E402
from src.pipelines.structure.planner import StructuredDocumentPlanner  # noqa: E402
from src.utils.config_loader import get_config  # noqa: E402

TASK = "eval"
TOP = 5


def build_index(source: Path, records: Path, config_path: Path, root: Path) -> BookIndexService:
    planner = StructuredDocumentPlanner.from_config(get_config(config_path))
    chunks = planner.plan(source.read_text(encoding="utf-8")).structured_chunks
    books = BookIndexService(root)
    books.build_source(TASK, chunks)
    books.update_records(TASK, chunks, json.loads(records.read_text(encoding="utf-8")))
    return books


def make_embedder(spec: str | None):
    if not spec:
        return None
    kind, _, model = spec.partition(":")
    if kind == "local":
        from src.infrastructure.embeddings.local import TransformersEmbedder

        return TransformersEmbedder(model)
    raise SystemExit(f"不支持的 --embedder: {spec}（例：local:BAAI/bge-small-en-v1.5）")


def rank_of(positions: list[int], expect: set[int]) -> int | None:
    return next((rank for rank, position in enumerate(positions, start=1) if position in expect), None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("records", type=Path)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    parser.add_argument("--embedder")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--dump", type=int, default=0)
    parser.add_argument("--workdir", type=Path, help="保留索引与向量缓存的目录（默认用临时目录，每次重算）")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        root = args.workdir or tmp
        books = build_index(args.source, args.records, args.config, Path(root))
        index = books.load(TASK)
        translated = [seg for seg in index.segments if seg.translation]
        print(f"索引：{len(index.segments)} 段，其中有译文 {len(translated)} 段", file=sys.stderr)
        if args.dump:
            step = max(1, len(translated) // args.dump)
            for seg in translated[::step][: args.dump]:
                print(
                    json.dumps(
                        {"position": seg.position, "source": seg.source, "translation": seg.translation},
                        ensure_ascii=False,
                    )
                )
            return 0

        cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
        lexical = SemanticSearchService(books, EmbedderSetup(None, ""))
        embedder = make_embedder(args.embedder)
        hybrid = SemanticSearchService(books, EmbedderSetup(embedder, "")) if embedder else None
        if hybrid:
            started = time.monotonic()
            count = hybrid.ensure_vectors(TASK)
            print(f"向量：{count} 条，{time.monotonic() - started:.1f}s", file=sys.stderr)

        rows = []
        for case in cases:
            expect = set(case["expect"])
            keyword = [hit["position"] for hit in books.search(TASK, case["query"], limit=TOP)["items"]]
            lex = [hit["position"] for hit in lexical.search(TASK, case["query"], TOP)["items"]]
            row = {
                "query": case["query"],
                "lang": query_language(case["query"]),
                "hard": bool(case.get("hard")),
                "expect": sorted(expect),
                "keyword": rank_of(keyword, expect),
                "lexical": rank_of(lex, expect),
            }
            if hybrid:
                result = hybrid.search(TASK, case["query"], TOP)
                row["hybrid"] = rank_of([hit["position"] for hit in result["items"]], expect)
                row["hybrid_mode"] = result["mode"]
            rows.append(row)

    methods = ["keyword", "lexical"] + (["hybrid"] if hybrid else [])
    summary = {}
    groups = {
        "en": lambda row: row["lang"] == "en" and not row["hard"],
        "zh": lambda row: row["lang"] == "zh" and not row["hard"],
        "hard_en": lambda row: row["lang"] == "en" and row["hard"],
        "hard_zh": lambda row: row["lang"] == "zh" and row["hard"],
        "all": lambda row: True,
    }
    for lang, keep in groups.items():
        subset = [row for row in rows if keep(row)]
        summary[lang] = {
            "n": len(subset),
            **{m: sum(1 for row in subset if row.get(m)) for m in methods},
            **{
                f"{m}_mrr": round(sum(1 / row[m] for row in subset if row.get(m)) / (len(subset) or 1), 3)
                for m in methods
            },
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.json:
        args.json.write_text(
            json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if args.report:
        lines = [
            "| 查询语言 | 用例数 | " + " | ".join(f"{m} 前5命中" for m in methods) + " |",
            "| --- | ---: | " + " | ".join("---:" for _ in methods) + " |",
        ]
        labels = (
            ("en", "英文·改写"),
            ("zh", "中文·改写"),
            ("hard_en", "英文·少重叠"),
            ("hard_zh", "中文·少重叠"),
            ("all", "合计"),
        )
        for lang, label in labels:
            s = summary[lang]
            lines.append(
                f"| {label} | {s['n']} | " + " | ".join(f"{s[m]}（{s[m] / (s['n'] or 1):.0%}）" for m in methods) + " |"
            )
        args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
