"""分类型修复：分路顺序、接受标准、约束式编辑、确定性标签修复，以及端到端的路由与拒绝。"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableLambda

from src.domain.document import inline_codec
from src.domain.rules import typed_repair as rules
from src.infrastructure.document import pandoc


@dataclass
class Issue:
    kind: str
    severity: str
    message: str


# --------------------------------------------------------------------------- #
# 纯规则
# --------------------------------------------------------------------------- #


def test_routes_follow_fixed_order_and_ignore_low_issues():
    issues = [
        Issue("terminology", "medium", "术语"),
        Issue("fidelity", "medium", "缺少原文数字: 7"),
        Issue("missing", "high", "缺段"),
        Issue("format", "low", "加粗数量变化"),
        Issue("structure", "low", "多余的闭合标签"),
    ]
    assert rules.routes_needed(issues) == ["retranslate", "number", "terminology"]


def test_accept_requires_target_gone_and_nothing_new():
    before = [Issue("terminology", "medium", "术语 A"), Issue("fidelity", "medium", "缺少原文数字: 7")]
    fixed = [Issue("fidelity", "medium", "缺少原文数字: 7"), Issue("format", "low", "提示")]
    assert rules.accept_repair("terminology", before, fixed) == (True, "")

    still = [Issue("terminology", "medium", "术语 A")]
    assert rules.accept_repair("terminology", before, still)[0] is False

    # 修好了术语，却新增了残留英文：修好一类、新增另一类 → 拒绝
    side_effect = [
        Issue("fidelity", "medium", "缺少原文数字: 7"),
        Issue("untranslated", "medium", "残留英文单词: however"),
    ]
    accepted, reason = rules.accept_repair("terminology", before, side_effect)
    assert accepted is False and "新增问题" in reason


def test_no_regression_for_whole_chunk_repair():
    before = [Issue("terminology", "medium", "术语 A"), Issue("fidelity", "medium", "数字")]
    assert rules.no_regression(before, [Issue("fidelity", "medium", "数字")]) is True
    assert rules.no_regression(before, before) is False  # 持平不接受
    assert rules.no_regression(before, [Issue("untranslated", "high", "新问题")]) is False


def test_term_edit_changes_nothing_but_the_term():
    text = "这台机器外在于国家机器，而<i1>使命</i1>不是。"
    edit = rules.Edit("台机器外在", "台战争机器外在")
    new, problems = rules.apply_edits("terminology", text, [edit], ["战争机器"])
    assert problems == [] and new == "这台战争机器外在于国家机器，而<i1>使命</i1>不是。"
    assert new.replace("战争机器", "机器", 1) == text  # 除了术语那一处，段内文字逐字不变

    rewrite = rules.Edit("这台机器外在于国家机器", "战争机器在国家之外运转")
    assert rules.check_edit("terminology", rewrite, text, ["战争机器"])  # 借术语之名改写整句：拒绝
    assert rules.check_edit("terminology", rules.Edit("机器", "战争机器"), text, ["战争机器"])  # 出现两次：拒绝
    assert rules.check_edit("terminology", rules.Edit("<i1>使命</i1>", "米申"), text, ["米申"])  # 碰标签：拒绝


def test_number_edit_is_limited_to_the_number():
    text = "准确率从 80 提升到 70。"
    assert rules.check_edit("number", rules.Edit("从 80 提升到 70", "从 70 提升到 80"), text, []) == ""
    assert rules.check_edit("number", rules.Edit("准确率从 80 提升到 70。", "精度大幅提升。"), text, [])


def test_canonicalize_tags_fixes_variants_and_strays_without_touching_text():
    source = pandoc.markdown_to_ast("The *CCRU* saw **it**.")["blocks"][0]["c"] if pandoc.pandoc_available() else None
    if source is None:
        pytest.skip("需要 pandoc")
    tagged = inline_codec.encode(source).text
    italic, bold = re.search(r"<(i\d+)>", tagged).group(1), re.search(r"<(b\d+)>", tagged).group(1)
    messy = f"《{italic}》CCRU《/{italic}》看见了<b9>它</b9></{italic}><{bold}>它</{bold}>。"
    cleaned = inline_codec.canonicalize_tags(messy, source)
    assert cleaned == f"<{italic}>CCRU</{italic}>看见了它<{bold}>它</{bold}>。"
    assert inline_codec.decode(cleaned, source).problems == []


# --------------------------------------------------------------------------- #
# 端到端：结构化翻译里的分路
# --------------------------------------------------------------------------- #

pandoc_only = pytest.mark.skipif(not pandoc.pandoc_available(), reason="需要 pandoc")
_SEG_RE = re.compile(r'<seg id="(\d+)">(.*?)</seg>', re.S)
_BLOCK_RE = re.compile(r"【段 (\d+)】\n原文：(.*?)\n当前译文：(.*?)\n问题：", re.S)


def _run(text, translator, checker, glossary=None, tmp_path=None):
    from src.core.translator import TranslationEngine
    from src.infrastructure.cache.translation_cache import TranslationCache
    from src.utils.config_loader import get_config

    class Factory:
        def create_translator(self):
            return RunnableLambda(translator)

        def create_checker(self):
            return RunnableLambda(checker)

        def create_analyst(self):
            return RunnableLambda(lambda _p: '{"summary": "测试"}')

    engine = TranslationEngine(
        glossary=glossary,
        model_factory=Factory(),
        cache=TranslationCache(tmp_path / "cache.db"),
        config=get_config(Path("config/config.yaml")),
        engine="native",
    )
    chunks = engine.plan_chunks(text)
    results = asyncio.run(engine.translate_batch(text, tmp_path / "out.md", prepared_chunks=chunks))
    return results, engine.structured_translator.repair_stats


def _segs(prompt_value):
    return _SEG_RE.findall(prompt_value.to_string().split("【待翻译段落】:")[-1])


@pandoc_only
def test_term_is_fixed_by_constrained_edit_and_rest_is_untouched(tmp_path):
    glossary = {"War Machine": "战争机器 (War Machine)"}
    first = "那台机器外在于国家，它从不停歇。"
    prompts = []

    def translator(prompt_value):
        return "\n".join(f'<seg id="{i}">{first}</seg>' for i, _ in _segs(prompt_value))

    def checker(prompt_value):
        prompts.append(prompt_value.to_string())
        return '<edit id="1"><from>那台机器外在</from><to>那台战争机器外在</to></edit>'

    results, stats = _run(
        "The War Machine is exterior to the state, and it never rests.\n", translator, checker, glossary, tmp_path
    )
    assert len(prompts) == 1 and "【修复类型】只修术语" in prompts[0]
    assert results[0].repaired and results[0].quality_report["passed"]
    assert "那台战争机器外在于国家，它从不停歇。" in results[0].translation
    assert stats.accepted["terminology"] == 1 and stats.model_calls == 1


@pandoc_only
def test_fix_that_adds_another_kind_of_problem_is_rejected_and_flagged(tmp_path):
    # 段 1 漏掉；审校员重译时夹了个英文词——修好了"缺段"，却新增"残留英文"
    def checker(prompt_value):
        assert "【修复类型】重译" in prompt_value.to_string()
        return '<seg id="1">该方法 however 没有提高准确率。</seg>'

    text = "The method does not improve accuracy.\n\nThe method improves accuracy.\n"
    results, stats = _translate_missing_first(text, checker, tmp_path)
    issues = results[0].quality_report["issues"]
    assert stats.rejected["retranslate"] == 1 and stats.accepted["retranslate"] == 0
    assert any(issue["kind"] == "repair" and "修复未采用" in issue["message"] for issue in issues)
    assert "however" not in results[0].translation


def _translate_missing_first(text, checker, tmp_path):
    def translator(prompt_value):
        segments = _segs(prompt_value)
        return "\n".join(f'<seg id="{i}">该方法提高了准确率。</seg>' for i, _ in segments if i != "1")

    return _run(text, translator, checker, None, tmp_path)


@pandoc_only
def test_tag_slips_are_fixed_without_calling_the_model(tmp_path):
    calls = []

    def translator(prompt_value):
        out = []
        for i, tagged in _segs(prompt_value):
            italic = re.search(r"<(i\d+)>", tagged).group(1)
            out.append(f'<seg id="{i}">《{italic}》CCRU《/{italic}》<b9>定义了</b9>超虚构</{italic}>。</seg>')
        return "\n".join(out)

    results, stats = _run(
        "The *CCRU* defined hyperstition.\n", translator, lambda p: calls.append(p) or "", None, tmp_path
    )
    assert calls == [] and stats.deterministic == 1 and stats.model_calls == 0
    assert "*CCRU*定义了超虚构" in results[0].translation.replace(" ", "")
    assert not any(issue["kind"] == "structure" for issue in results[0].quality_report["issues"])


@pandoc_only
def test_injected_error_bench_accepts_no_fix_that_breaks_something_else():
    from src.evaluation.repair_bench import run

    report = run(Path("docs/evals/datasets/injection_pairs.json"), max_fix_attempts=1)
    assert sum(report["injected"].values()) > 0 and report["sloppy_fixes_offered"] > 0
    assert report["sloppy_fixes_accepted"] == 0


def test_complete_output_missing_a_segment_is_not_called_truncated():
    from src.pipelines.translate.structured_translation import looks_truncated

    assert not looks_truncated('<seg id="1">甲</seg>\n<seg id="3">丙</seg>', None)
    assert looks_truncated('<seg id="1">甲</seg>\n<seg id="2">乙', None)
