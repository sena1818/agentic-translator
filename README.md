# agentic-translator

**An agentic translation system for dense, terminology-heavy books and papers — PDF/EPUB/Markdown in, reviewable Chinese Markdown out.**

Not tied to any single domain: bring your own glossary and translate professional texts from any field that breaks naive "chunk-and-prompt" translation — postmodern philosophy (Nick Land's CCRU writings, Reza Negarestani's *Cyclonopedia*), machine learning papers, or anything where terminology drift across 300+ chunks is unacceptable.

[![CI](https://github.com/sena1818/agentic-translator/actions/workflows/ci.yml/badge.svg)](https://github.com/sena1818/agentic-translator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

**English** | [简体中文](README.zh-CN.md)

**[Live demo →](https://sena1818.github.io/agentic-translator/)** a linked bilingual reader built from a public-domain text (Thoreau's *Walden*, excerpt), plus [EPUB](https://sena1818.github.io/agentic-translator/walden.epub) and [bilingual EPUB](https://sena1818.github.io/agentic-translator/walden.bilingual.epub) downloads.

![Web UI](docs/images/review-workspace.jpg)

## Why agentic?

A single prompt cannot hold a whole book. This system splits the work across three LLM roles orchestrated by a document-level **LangGraph StateGraph**:

- **Analyst** — samples the beginning, middle, and end within a fixed input budget, produces a global profile (summary, style, terminology hints) injected into every chunk's prompt.
- **Translator** — translates each chunk concurrently, constrained by the glossary and the document profile.
- **Reviewer** — steps in *only* for chunks that fail quality checks (untranslated residue, missing glossary terms, broken Markdown structure), keeping cost proportional to risk.

Every claim about whether this architecture helps is backed by a reproducible [evaluation harness](#evaluation) — with a threshold for deciding whether RAG translation memory is warranted; no candidate phrases means insufficient data ([ADR-0002](docs/adr/0002-rag-translation-memory-threshold.md)).

## Features

- 🧱 **Structure-preserving translation (Document IR)** — the document is held as a pandoc AST; the LLM only sees text with inline tags (`<b1>…</b1>`, `<m2/>`), so formulas, link URLs, images, footnotes, code, heading levels and anchors cannot be dropped or rewritten. Every segment is checked deterministically against the source skeleton; only failing segments go to the Reviewer, and anything still broken falls back to the source text instead of shipping a lossy translation ([ADR-0004](docs/adr/0004-document-ir-structured-translation.md))
- 🔗 **Linked bilingual reader** — a single offline HTML file: click any paragraph to jump to its counterpart (both directions), scroll-follow by segment anchors, translated table of contents, footnote popovers, MathML formulas, and `n`/`p` navigation through segments that need review
- 🕸️ **Document-level LangGraph engine** — analyst → Send-API fan-out → per-chunk translate/QA/repair → aggregate, in one StateGraph ([ADR-0001](docs/adr/0001-langgraph-document-level-graph.md))
- 🚀 **Async concurrency** — account-wide request concurrency and rolling RPM limits shared across workers and exponential-backoff retry
- 🧩 **Chapter-aware chunking** — chunks planned along Markdown headings with stable structured chunk IDs
- 📚 **Glossary enforcement** — JSON glossaries injected into prompts and verified by the QA checker
- 🛡️ **Selective repair** — QA flags high-risk chunks; only those get a one-shot Reviewer retranslation
- 💾 **Chunk-level SQLite cache** — resumable by design; keys include model, glossary, prompt version, and document-profile fingerprint
- ⚠️ **Failure isolation** — a failed chunk becomes a placeholder, never a stalled document
- 🌗 **Bilingual export** — paragraph-aligned source/translation Markdown plus the linked reader above; the reader opens inside the web app at a bookmarkable address, and the offline file stores each image once for both panes
- 📖 **EPUB export** — translation-only and bilingual EPUB3 with table of contents, images and linked footnotes; images that cannot be found are listed in the export stats and shown on the task page
- 🌐 **Web UI** — React + FastAPI, with task search, cancellation, recovery, per-chunk retranslation, and manual review
- 🔌 **MCP server** — stdio transport exposing translation, task management, glossary, review, terminology and audit tools to Claude Desktop and other MCP clients; internal agents use the same tool registry
- 🔭 **Observability** — opt-in Langfuse tracing of the full agent call chain and per-chunk token usage, degrading to no-op when unconfigured

## Quick start (Docker)

No local Python/Node needed — one command brings up the frontend, API, and worker:

```bash
git clone https://github.com/sena1818/agentic-translator.git
cd agentic-translator

# 1. Configure the API key
cp .env.example .env      # then fill in a real SILICONFLOW_API_KEY

# 2. Build and start everything
docker compose up --build

# 3. Open the browser
#    Web UI:   http://localhost:8000
#    API docs: http://localhost:8000/docs
```

The **api** service serves the Web UI and REST API; the **worker** service consumes the translation queue independently — both share one image. Task DB, translation cache, uploads, and results live in named volumes (`translator-data` / `translator-logs`), so `docker compose down && docker compose up` keeps your data; use `down -v` to wipe it.

The default image installs Pandoc for Markdown/EPUB. For PDF support, build the optional Marker dependencies with `ENABLE_PDF=1 docker compose build`, then run `docker compose up`. The UI only offers formats supported by the installed tools.

## Translation and review workflow

Open a task's own URL to follow conversion, analysis, translation, and export. Search, status filters, and pagination help navigate the queue. Review source/translation pairs and quality issues, edit a chunk to update downloads, or retranslate selected/failed chunks while preserving the other results. Recovery reuses the task's configuration and glossary snapshots.

Execution leases and version checks prevent cancelled or expired workers from publishing over newer results. See [ADR-0003](docs/adr/0003-task-leases-and-review-artifacts.md).

## Quick start (local)

```bash
git clone https://github.com/sena1818/agentic-translator.git
cd agentic-translator
pip install -r requirements.txt
cp .env.example .env      # fill in SILICONFLOW_API_KEY

# Translate a Markdown file
python translate.py data/input/book.md

# EPUB with a glossary, custom output path
python translate.py BookTrans/Cyclonopedia.epub \
  -g data/glossaries/CPglossary.json \
  -o output_final/Cyclonopedia_CN.md

# Bilingual output
python translate.py data/input/book.md --bilingual --skip-conversion

# Draft a glossary from the document (review, edit, then pass with -g)
python translate.py data/input/book.md --suggest-glossary --skip-conversion \
  -o data/glossaries/book_draft.json
```

Don't have a glossary yet? `--suggest-glossary` has a terminology agent scan the
document and draft candidate terms with suggested translations, so bringing your
own glossary starts from a first pass instead of a blank file.

For the Web UI locally:

```bash
python run_server.py           # API + inline worker on :8000
# optional dev frontend:
cd frontend && npm install && npm run dev   # Vite dev server on :5173
```

For long-running jobs, split API and workers:

```bash
TRANSLATION_INLINE_WORKER=0 python run_server.py     # terminal 1: API only
python run_worker.py --processes 4 --parallel-tasks 2 # terminal 2: worker pool
# or on macOS: bash scripts/longrun.sh                # background API + worker + PID/log files
```

## Architecture

### Layered layout

```
┌─────────────────────── entry points ───────────────────────┐
│  translate.py (CLI)   src/api (FastAPI + worker)            │
│  src/interfaces/mcp (MCP stdio server)                      │
├──────────────────────── pipelines ──────────────────────────┤
│  src/pipelines: preprocess → translate →           │
│  postprocess.  translate/ holds the engine:                 │
│    graph_engine.py (LangGraph StateGraph)                   │
│  plus prompt_builder / translation_client /                 │
│  quality_pipeline / document_analyzer                       │
├───────────────────── domain & core ─────────────────────────┤
│  src/domain: models, rules (pure, no I/O)        │
│  src/core: chunk_planner, translation_cache, rate_limiter,  │
│            output_manager (ordered streaming), validator    │
├────────────────────── infrastructure ───────────────────────┤
│  src/infrastructure: llm (model factory), cache,            │
│  persistence, converters (Pandoc), observability (Langfuse),│
│  filesystem                                         │
├──────────────────────── evaluation ─────────────────────────┤
│  src/evaluation: translator-eval CLI — LLM-as-judge,        │
│  paired comparisons, consistency-rate metric                │
└─────────────────────────────────────────────────────────────┘
```

### The LangGraph document graph

```
        START
          │
      ┌───▼────┐   document profile: summary / style / term hints
      │analyze │   (Analyst role)
      └───┬────┘
          │  Send API fan-out — one branch per chunk
   ┌──────┼─────────┬─ ··· ─┐
┌──▼───┐ ┌▼─────┐ ┌─▼────┐
│transl│ │transl│ │transl│   each branch: translate → QA check
│ate #0│ │ate #1│ │ate #N│   → (fail?) Reviewer repair → re-check
└──┬───┘ └┬─────┘ └─┬────┘   failures collapse to placeholders
   └──────┼─────────┴────┘
      ┌───▼─────┐  stats only — chunks already streamed to disk
      │aggregate│  in source order by OutputManager
      └───┬─────┘
         END
```

Design decisions worth noting (full rationale in [ADR-0001](docs/adr/0001-langgraph-document-level-graph.md)):

- **No LangGraph checkpointer** — resume is owned by the chunk-level SQLite cache; graph-state persistence would duplicate it.
- **Ordered streaming survives crashes** — chunks are written to disk in source order as they finish, so a crash mid-book still leaves a readable partial result.
- **Single engine** — the hand-written asyncio engine that the graph replaced has been removed; a contract test suite pins the output file, result list and progress events of `translate_batch`.

## Evaluation

`translator-eval` runs two paired comparisons — each toggles exactly one switch off the production baseline (multi-agent on + glossary on):

| Comparison | Variant A | Variant B |
| --- | --- | --- |
| Multi-agent roles | on | off (translator only) |
| Glossary | on | off |

- **Judge**: Gemini (deliberately a different model family from the DeepSeek contestant, to avoid same-family preference), scoring accuracy / fluency / terminology on a 1–5 Likert scale. Judge prompt is versioned at [docs/evals/judge_prompt.md](docs/evals/judge_prompt.md).
- **Consistency-rate metric**: measures whether repeated off-glossary phrases translate identically across chunks. It gates whether RAG translation memory is worth building at all — threshold 0.90, decision recorded in [ADR-0002](docs/adr/0002-rag-translation-memory-threshold.md).
- **Reproduce**: `translator-eval --out docs/evals/results` (requires `SILICONFLOW_API_KEY` + `GOOGLE_API_KEY`); `--dry-run` smoke-tests the harness with fake models.

Methodology and datasets live in [docs/evals/](docs/evals/methodology.md). The `translator-eval` report under `docs/evals/results/report.md` is still a `--dry-run` smoke run (placeholder scores).

**Real before/after benchmark** ([bench_2026-11-12.md](docs/evals/results/bench_2026-11-12.md)): the pre-round-two version vs. the current default vs. everything optional switched on, on excerpts from four books (973 segments, DeepSeek V4.1 Flash):

- Current default ≈ old version: same cost, same structure (no missing segments, no failed chunks), flagged-segment rate 9.2% vs. 9.0%. In the blind pairwise judging it won 18 and lost 16 (p ≈ 0.86).
- All optional stages on: 6.4× the cost and about 15× the wall time, for a flagged rate of 8.6% and no significant judge preference. The term agent and the review agent account for most of that cost, so the optional stages stay off by default ([cost-switches.md](docs/cost-switches.md)).

Throughput on a real production run (Ccru.md, 344 chunks, concurrency 10): **~25 min end-to-end, 13.8 chunks/min, 344/344 succeeded**.

## MCP server

Expose the whole system to Claude Desktop (or any MCP client) via stdio: instant text translation, async document jobs, glossary CRUD, plus review, terminology/correction-memory, audit, usage and search tools. Internal agents and MCP share one tool registry, and every tool that writes data only previews until called with `confirm: true`. Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agentic-translator": {
      "command": "python",
      "args": ["-m", "src.interfaces.mcp.server"],
      "cwd": "/absolute/path/to/agentic-translator",
      "env": { "SILICONFLOW_API_KEY": "sk-your-key-here" }
    }
  }
}
```

Tool list, workflows, and cancellation semantics: [docs/mcp-server.md](docs/mcp-server.md).

## Observability

Opt-in Langfuse tracing of the agent call chain (analyst / translator / reviewer) with per-chunk token usage. Enable in [config/config.yaml](config/config.yaml) and set `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`; anything missing degrades to a no-op with a warning — the pipeline never depends on it. Details: [docs/observability.md](docs/observability.md).

## Configuration

Everything lives in [config/config.yaml](config/config.yaml). The switches you'll actually touch:

```yaml
api:
  model: "deepseek-ai/DeepSeek-V3"
  translator:
    temperature: 0.3

concurrency:
  max_concurrent_requests: 10   # lower to 5 if you hit 429s
  rate_limit_per_minute: 200

multi_agent:
  enabled: true

observability:
  langfuse:
    enabled: false
```

## Glossaries

JSON maps of English term → Chinese rendering, enforced in prompts and checked by QA:

```json
{
  "Hyperstition": "超虚构 (Hyperstition)",
  "War Machine": "战争机器 (War Machine)"
}
```

Pass with `-g data/glossaries/my_glossary.json`, or manage them visually in the Web UI / via MCP tools.

## Development

```bash
pip install -e ".[dev]"   # or: pip install -r requirements.txt
ruff check .              # lint
pytest                    # unit + integration tests
python scripts/generate_api_types.py --check
cd frontend && npm ci && npm run lint && npm test && npm run build
```

CI runs Ruff, pytest, and the frontend build on every push ([ci.yml](.github/workflows/ci.yml)).

More docs: [CONTEXT.md](CONTEXT.md) (domain glossary) · [docs/adr/](docs/adr/) (architecture decisions) · [docs/guides/](docs/guides/) (中文使用指南).

## License

MIT — see [LICENSE](LICENSE).
