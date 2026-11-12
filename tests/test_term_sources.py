"""多路术语候选：关键度、结构线索、多处上下文、歧义信号与账本分批。词频用注入的假表，不依赖 wordfreq 数值。"""

from __future__ import annotations

import asyncio
from collections import Counter
from types import SimpleNamespace

from langchain_core.runnables import RunnableLambda

from src.domain.index.book_index import STATUS_TRANSLATED, BookIndex, IndexedSegment
from src.domain.rules import term_sources as ts
from src.domain.rules.term_sources import SourceConfig, gather_candidates, paragraph_index

RARE = {
    "polytics": 0.0,
    "trison": 0.0,
    "trisons": 0.0,
    "petropolitics": 0.0,
    "parsani": 0.0,
    "openness": 3.0,
    "radical openness": 2.0,
    "hole complex": 2.0,
    "mission": 4.6,
    "gates": 4.4,
    "xerodrome": 0.0,
}


def fake_zipf(text: str) -> float:
    text = text.lower()
    if text in RARE:
        return RARE[text]
    words = text.split()
    if len(words) > 1:
        return min(fake_zipf(word) for word in words)
    return 6.0  # 表外一律当作极常见词


def _index(paragraphs: list[str], sections: list[tuple[str, ...]] | None = None) -> BookIndex:
    sections = sections or [()] * len(paragraphs)
    return BookIndex(
        IndexedSegment(i, 0, text, section_path=section)
        for i, (text, section) in enumerate(zip(paragraphs, sections, strict=True))
    )


BOOK = [
    "Polytics is what Parsani calls the politics of the swarm. Parsani writes about it.",
    "The trisons gather; a trison is a triple cell. Parsani writes again and polytics returns.",
    "Radical openness is not openness to the other. Parsani writes that polytics needs radical openness.",
    "Here the hole complex appears, and the hole complex spreads. Parsani writes of trisons and polytics.",
    "Openness again, openness everywhere, openness without end. Radical openness and the hole complex.",
    "Parsani, Parsani, Parsani: the notes of Parsani, the cult of Parsani, the dust of Parsani, Parsani and Parsani.",
    "Parsani once more. Then Parsani, and Parsani, and Parsani, and finally Parsani.",
]


def test_keyness_finds_lowercase_coinages_and_merges_plurals():
    found = ts.keyness_candidates("\n\n".join(BOOK), fake_zipf, SourceConfig(min_keyness_count=3))
    assert "polytics" in found and found["polytics"].count == 4
    assert "trison" in found and "trisons" not in found  # 复数并到单数
    assert "radical openness" in found and "hole complex" in found
    assert "parsani writes" not in found  # 专名 + 常见动词的搭配不要
    assert "politics" not in found and "again" not in found  # 常见词不要


def test_structure_cues_come_from_emphasis_quotes_and_headings():
    blocks = [
        {
            "t": "Header",
            "c": [2, ["", [], []], [{"t": "Str", "c": "Hole"}, {"t": "Space"}, {"t": "Str", "c": "Complex"}]],
        },
        {
            "t": "Para",
            "c": [
                {"t": "Emph", "c": [{"t": "Str", "c": "trison"}]},
                {"t": "Space"},
                {
                    "t": "Quoted",
                    "c": [
                        {"t": "DoubleQuote"},
                        [
                            {"t": "Str", "c": "being"},
                            {"t": "Space"},
                            {"t": "Str", "c": "open"},
                            {"t": "Space"},
                            {"t": "Str", "c": "to"},
                        ],
                    ],
                },
            ],
        },
    ]
    cues = ts.structure_cues(blocks)
    assert cues[("header", "Hole Complex")] == 1 and cues[("emph", "trison")] == 1
    found = ts.structure_candidates(cues, "\n\n".join(BOOK), fake_zipf)
    assert "hole complex" in found and found["hole complex"][1] == 3
    assert "trison" in found
    assert ts.clean_cue("being open to", fake_zipf) is None  # 虚词结尾
    assert ts.clean_cue("it", fake_zipf) is None
    assert ts.clean_cue("Xerodrome (AQ=198)", fake_zipf) == "Xerodrome"


def test_contexts_prefer_first_definitional_and_other_sections():
    paragraphs = [
        "Polytics opens the book.",
        "Later polytics repeats in the same chapter.",
        "Polytics is a politics of swarms, as the glossary says.",
        "In chapter two polytics returns.",
        "Chapter three: polytics again.",
    ]
    sections = [("One",), ("One",), ("One",), ("Two",), ("Three",)]
    hits = ts._hits(_index(paragraphs, sections), "polytics")
    contexts = ts.select_contexts(hits, k=4, chars=60)
    assert len(contexts) == 4
    assert contexts[0].startswith("〔One〕Polytics opens")
    assert any("is a politics of swarms" in text for text in contexts)  # 带定义意味的
    assert any(text.startswith("〔Two〕") for text in contexts) and any(
        text.startswith("〔Three〕") for text in contexts
    )
    assert len(ts.select_contexts(hits[:1], k=4)) == 1


def test_ambiguity_signals():
    paragraphs = [
        "He had a special mission. The mission failed.",
        "Captain Mission sailed. Later Captain Mission returned to Libertatia.",
        "Bill Gates laughed. Bill Gates left.",
        "Polytics rules. Polytics again.",
    ]
    index = _index(paragraphs)
    mission = ts.ambiguity_signals("Mission", ts._hits(index, "Mission"), fake_zipf)
    assert ts.MIXED_CASE in mission and ts.COMMON_WORD in mission
    assert ts.COMMON_WORD in ts.ambiguity_signals("Gates", ts._hits(index, "Gates"), fake_zipf)
    assert ts.SENTENCE_INITIAL in ts.ambiguity_signals("Polytics", ts._hits(index, "Polytics"), fake_zipf)
    assert (
        ts.ambiguity_signals("Polytics", ts._hits(index, "Polytics"), fake_zipf, ["聚落政治", "多元政治"])[-1]
        == ts.DIVERGENT
    )


def test_divergent_renderings_are_read_from_the_index():
    segments = [
        IndexedSegment(0, 0, "Libertatia rose.", "自由国崛起。", status=STATUS_TRANSLATED),
        IndexedSegment(1, 0, "Then Libertatia fell.", "然后利伯塔提亚陷落。", status=STATUS_TRANSLATED),
        IndexedSegment(2, 0, "Libertatia, again.", "自由国，又一次。", status=STATUS_TRANSLATED),
        IndexedSegment(3, 0, "Libertatia, once more.", "利伯塔提亚，又一次。", status=STATUS_TRANSLATED),
    ]
    candidates = gather_candidates(BookIndex(segments), {}, zipf=fake_zipf)
    libertatia = next(c for c in candidates if c.term == "Libertatia")
    assert ts.DIVERGENT in libertatia.ambiguity


def test_gather_keeps_proper_nouns_unsplit_and_adds_concepts_with_contexts():
    text = (
        "They met in New York twice. Later they left New York for good, and in New York again.\n\n"
        "The Sunken Track runs west; the Sunken Track is old. Despite this, Parsani left.\n\n"
        "Later Parsani returned, and Parsani wrote.\n\n" + "\n\n".join(BOOK)
    )
    index = paragraph_index(text)
    config = SourceConfig(min_keyness_count=3)
    candidates = gather_candidates(index, {"Hole complex": "孔洞复合体"}, text=text, zipf=fake_zipf, config=config)
    terms = {c.term: c for c in candidates}
    assert {"New York", "Sunken Track", "Parsani", "polytics", "radical openness"} <= set(terms)
    assert not set(terms) & {"York", "New", "Track", "Sunken", "Later Parsani", "hole complex"}  # 不拆专名；术语表已有
    assert terms["polytics"].sources == ("keyness",)
    assert "proper" in terms["Parsani"].sources and "keyness" in terms["Parsani"].sources
    assert 2 <= len(terms["polytics"].contexts) <= 5 and terms["polytics"].context == terms["polytics"].contexts[0]


def test_proper_source_alone_matches_the_old_extractor():
    from src.domain.rules.term_candidates import extract_term_candidates

    text = "\n\n".join(BOOK) + "\n\nHamid Parsani and the AOE. The AOE knew Hamid Parsani."
    old = [c.term for c in extract_term_candidates(text, {}, max_terms=120)]
    new = gather_candidates(
        paragraph_index(text), {}, text=text, zipf=fake_zipf, config=SourceConfig(sources=("proper",))
    )
    assert [c.term for c in new] == old


def test_quotas_and_total_cap():
    text = "\n\n".join(BOOK)
    config = SourceConfig(min_keyness_count=3, max_keyness=1, max_terms=2)
    candidates = gather_candidates(paragraph_index(text), {}, text=text, zipf=fake_zipf, config=config)
    assert len(candidates) <= 2
    assert sum(1 for c in candidates if c.sources == ("keyness",)) <= 1


def test_paragraph_index_tracks_headings():
    index = paragraph_index("# Part One\n\nAlpha text.\n\n## Chapter A\n\nBeta *text*.\n\n# Part Two\n\nGamma.")
    paths = [(seg.source, seg.section_path) for seg in index.segments]
    assert ("Beta text.", ("Part One", "Chapter A")) in paths
    assert ("Gamma.", ("Part Two",)) in paths


def _ledger_config(**overrides):
    values = {
        "api.translator.max_retries": 0,
        "multi_agent.term_ledger.batch_size": 2,
        "multi_agent.term_ledger.sources": ["proper", "keyness"],
        **overrides,
    }
    return SimpleNamespace(get=lambda key, default=None: values.get(key, default))


def test_ledger_batches_calls_and_shows_contexts_and_ambiguity(monkeypatch):
    from src.pipelines.glossary.term_ledger import TermLedgerBuilder
    from src.pipelines.translate.prompt_builder import TranslationPromptBuilder

    monkeypatch.setattr(ts, "default_zipf", lambda: fake_zipf)
    prompts = []

    def llm(prompt_value):
        text = prompt_value.to_string()
        prompts.append(text)
        names = [
            line[2:].split("（", 1)[0] for line in text.splitlines() if line.startswith("- ") and "次；来源" in line
        ]
        return (
            '{"terms": [' + ",".join(f'{{"term": "{n}", "translation": "译{n}", "keep": true}}' for n in names) + "]}"
        )

    text = "\n\n".join(
        BOOK + ["He had a special mission. The mission failed.", "Captain Mission sailed. Captain Mission returned."]
    )
    config = _ledger_config()
    builder = TermLedgerBuilder(
        RunnableLambda(llm), config, TranslationPromptBuilder(SimpleNamespace(get=lambda k, d=None: d))
    )
    ledger = asyncio.run(builder.build(text, {}))
    assert builder.usage.calls == len(prompts) >= 2  # 分批
    assert builder.usage.estimated_prompt_tokens > 0 and builder.usage.by_source["keyness"] >= 1
    assert "polytics" in ledger and "Mission" in ledger
    mission_prompt = next(p for p in prompts if "- Mission（" in p)
    assert "可能有歧义：" in mission_prompt and "同时是常用英语词" in mission_prompt
    assert "    2. " in mission_prompt  # 多处上下文

    deferred = TermLedgerBuilder(
        RunnableLambda(llm),
        _ledger_config(**{"multi_agent.term_ledger.defer_ambiguous": True}),
        TranslationPromptBuilder(SimpleNamespace(get=lambda k, d=None: d)),
    )
    ledger = asyncio.run(deferred.build(text, {}))
    assert "Mission" not in ledger and any(c.term == "Mission" for c in deferred.deferred)
    assert deferred.usage.deferred == len(deferred.deferred)


def test_structure_cues_counter_is_plain_data():
    assert isinstance(ts.structure_cues([]), Counter)
