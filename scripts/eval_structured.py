"""用真实模型跑结构化翻译，统计一次通过率 / 修复 / 回退 / token。

用法: python scripts/eval_structured.py <输出目录> <源文件.md>...（读取 config/gemini.yaml）
"""

import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

os.environ.setdefault("GEMINI_API_KEY", "proxy-injected")  # 真实密钥由网络代理注入
sys.path.insert(0, os.getcwd())

from langchain_core.callbacks import get_usage_metadata_callback  # noqa: E402

from src.core.translator import TranslationEngine  # noqa: E402
from src.infrastructure.cache.translation_cache import TranslationCache  # noqa: E402
from src.pipelines.structure.exporter import StructuredExporter  # noqa: E402
from src.pipelines.translate import structured_translation as st  # noqa: E402
from src.utils.config_loader import get_config  # noqa: E402

if len(sys.argv) < 3:
    sys.exit("用法: python scripts/eval_structured.py <输出目录> <源文件.md>...")
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
SOURCES = [Path(p) for p in sys.argv[2:]]

attempts = defaultdict(list)  # seg_id -> [issues per attempt]
_orig_check = st.SegmentValidator.check


def recording_check(self, segment, source_inlines, tagged):
    outcome = _orig_check(self, segment, source_inlines, tagged)
    attempts[(CURRENT, segment.seg_id)].append(
        {"tagged": tagged, "issues": [(i.kind, i.severity, i.message) for i in outcome.issues]}
    )
    return outcome


st.SegmentValidator.check = recording_check
CURRENT = None


async def main():
    global CURRENT
    config = get_config(Path("config/gemini.yaml"))
    glossary = json.loads(Path("data/glossaries/CPglossary.json").read_text())
    summary = []
    for source in SOURCES:
        CURRENT = source.stem
        text = source.read_text(encoding="utf-8")
        engine = TranslationEngine(glossary=glossary, config=config, cache=TranslationCache(OUT / "cache.db"))
        chunks = engine.plan_chunks(text)
        start = time.time()
        with get_usage_metadata_callback() as cb:
            results = await engine.translate_batch(text, OUT / f"{source.stem}.stream.md", prepared_chunks=chunks)
        elapsed = time.time() - start
        stats = StructuredExporter.from_config(config).export_all(
            text, [asdict(r) for r in results], OUT, source.stem, source.stem, bilingual=True
        )
        (OUT / f"{source.stem}.records.json").write_text(
            json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2)
        )
        segs = {k: v for k, v in attempts.items() if k[0] == source.stem}
        first_clean = sum(
            1 for v in segs.values() if not any(sev in ("high", "medium") for _, sev, _ in v[0]["issues"])
        )
        first_struct = sum(
            1
            for v in segs.values()
            if not any(k in ("structure", "missing") and s in ("high", "medium") for k, s, _ in v[0]["issues"])
        )
        repaired_attempted = sum(1 for v in segs.values() if len(v) > 1)
        fallback = sum(
            1 for r in results for i in (r.quality_report or {}).get("issues", []) if "保留原文" in i["message"]
        )
        summary.append(
            {
                "source": source.name,
                "chars": len(text),
                "chunks": len(chunks),
                "segments": len(segs),
                "first_pass_clean": first_clean,
                "first_pass_structure_ok": first_struct,
                "sent_to_repair": repaired_attempted,
                "fallback_to_source": fallback,
                "chunks_failed": sum(not r.success for r in results),
                "final_issues": sum(len((r.quality_report or {}).get("issues", [])) for r in results),
                "dead_links": stats["dead_links"] if stats else None,
                "aligned": stats and stats["aligned_chunks"],
                "seconds": round(elapsed, 1),
                "usage": {
                    m: {k: u.get(k) for k in ("input_tokens", "output_tokens", "total_tokens")}
                    for m, u in cb.usage_metadata.items()
                },
            }
        )
        print(json.dumps(summary[-1], ensure_ascii=False), flush=True)
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    (OUT / "attempts.json").write_text(
        json.dumps({f"{k[0]}#{k[1]}": v for k, v in attempts.items()}, ensure_ascii=False, indent=2)
    )


asyncio.run(main())
