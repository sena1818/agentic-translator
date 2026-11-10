import pytest

from src.core.translator import DocumentProfile, TranslationEngine
from src.core.validator import TranslationValidator
from src.domain.rules.chunk_planning import TextChunk
from src.domain.rules.glossary_selection import select_relevant_glossary, term_appears_in


def test_select_relevant_glossary_keeps_only_present_terms():
    glossary = {
        "Hyperstition": "超虚构 (Hyperstition)",
        "War Machine": "战争机器 (War Machine)",
        "Gradient Descent": "梯度下降 (Gradient Descent)",
    }
    selected = select_relevant_glossary(
        glossary, "The war machine drives hyperstition.", ""
    )
    assert set(selected) == {"Hyperstition", "War Machine"}


def test_select_relevant_glossary_is_case_insensitive_and_uses_context():
    glossary = {"Backpropagation": "反向传播 (Backpropagation)"}
    assert select_relevant_glossary(glossary, "no term here", "backpropagation appears") == glossary
    assert select_relevant_glossary(glossary, "", "") == {}
    assert select_relevant_glossary({}, "backpropagation") == {}


def test_select_relevant_glossary_ignores_substrings_inside_words():
    glossary = {"AI": "人工智能 (AI)", "Oil": "石油 (Oil)"}
    assert select_relevant_glossary(glossary, "He said the boiling water was hot.") == {}


@pytest.mark.parametrize(
    ("term", "text"),
    [
        ("AI", "He said nothing."),
        ("Oil", "The water was boiling."),
        ("Oil", "Turmoil everywhere."),
        ("Land", "Islands drift apart."),
        ("Art", "Start the engine."),
        ("Model", "Remodeling the house."),
        ("Machine", "Machinery hums."),
        ("War Machine", "The war machinery grinds."),
        ("Body without Organs", "body without organism"),
        ("AI", ""),
        ("", "anything"),
    ],
)
def test_term_appears_in_rejects_non_matches(term, text):
    assert not term_appears_in(term, text)


@pytest.mark.parametrize(
    ("term", "text"),
    [
        ("AI", "AI systems are everywhere."),
        ("AI", "Is it ai?"),
        ("Oil", "Crude OIL prices rose."),
        # 复数
        ("Machine", "Desiring machines produce."),
        ("Process", "Several processes ran."),
        ("Ontology", "Flat ontologies abound."),
        ("Ontology", "A flat ontology."),
        # 所有格
        ("AI", "The AI's output was odd."),
        ("Oil", "Oil’s politics."),
        ("Machine", "The machines' noise."),
        # 多词术语：空格 / 连字符 / 换行互通
        ("War Machine", "The war machine advances."),
        ("War Machine", "war-machines everywhere"),
        ("War Machine", "the War\nMachine"),
        ("Body-without-Organs", "the body without organs"),
        ("Body without Organs", "the Body-without-Organs's plane"),
        # 标点与 Markdown 包裹
        ("Hyperstition", "**Hyperstition**, again."),
        ("Hyperstition", "(hyperstition)"),
        ("C++", "Written in C++ mostly."),
    ],
)
def test_term_appears_in_accepts_matches(term, text):
    assert term_appears_in(term, text)


def test_select_relevant_glossary_matches_inflections_and_multiword_terms():
    glossary = {
        "War Machine": "战争机器 (War Machine)",
        "Ontology": "本体论 (Ontology)",
        "AI": "人工智能 (AI)",
        "Oil": "石油 (Oil)",
    }
    selected = select_relevant_glossary(
        glossary, "War-machines and flat ontologies.", "The AI's view."
    )
    assert list(selected) == ["War Machine", "Ontology", "AI"]


def test_validator_glossary_check_uses_same_matching_rule():
    validator = TranslationValidator()
    glossary = {"AI": "人工智能 (AI)", "Oil": "石油 (Oil)"}

    # 原文不含术语（仅为子串）：不应报术语缺失
    issues = validator._check_glossary("He said the boiling water was hot.", "他说开水很烫。", glossary)
    assert issues == []

    # 原文含复数 / 所有格形式：应检查并在缺失译名时报告
    issues = validator._check_glossary("The AI's oils.", "它的东西。", glossary)
    assert {issue.kind for issue in issues} == {"terminology"}
    assert len(issues) == 2

    issues = validator._check_glossary("The AI's oils.", "人工智能的石油。", glossary)
    assert issues == []


def _chunk(text: str) -> TextChunk:
    return TextChunk(
        index=0,
        chunk_id="chunk-0",
        text=text,
        section_path=["Section"],
        section_title="Section",
        context_text="",
    )


def test_cache_key_unaffected_by_unrelated_glossary_term():
    engine = TranslationEngine.__new__(TranslationEngine)
    engine.prompt_version = "v4"
    engine.document_profile = DocumentProfile(summary="profile-a")

    class Config:
        model_name = "test-model"
        prompt_fingerprint = "test-prompt-fingerprint"

    engine.config = Config()
    chunk = _chunk("Hyperstition accelerates myth.")

    engine.glossary = {"Hyperstition": "超虚构 (Hyperstition)"}
    key_before = TranslationEngine._build_cache_key(engine, chunk)

    # 新增一个本块不出现的术语，不应使该块缓存失效
    engine.glossary = {
        "Hyperstition": "超虚构 (Hyperstition)",
        "Gradient Descent": "梯度下降 (Gradient Descent)",
    }
    key_after = TranslationEngine._build_cache_key(engine, chunk)

    assert key_before == key_after


def test_cache_key_changes_when_relevant_term_changes():
    engine = TranslationEngine.__new__(TranslationEngine)
    engine.prompt_version = "v4"
    engine.document_profile = DocumentProfile(summary="profile-a")

    class Config:
        model_name = "test-model"
        prompt_fingerprint = "test-prompt-fingerprint"

    engine.config = Config()
    chunk = _chunk("Hyperstition accelerates myth.")

    engine.glossary = {"Hyperstition": "超虚构 (Hyperstition)"}
    key_before = TranslationEngine._build_cache_key(engine, chunk)

    engine.glossary = {"Hyperstition": "超虚拟"}
    key_after = TranslationEngine._build_cache_key(engine, chunk)

    assert key_before != key_after
