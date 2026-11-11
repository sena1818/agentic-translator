"""注入错误基准：注入器只做声明的那一种改动；统计口径只计新增问题；一条命令可跑通且不需密钥。"""

from __future__ import annotations

import json
from pathlib import Path

from src.evaluation import injection
from src.evaluation.injection import Pair, inject_all, load_dataset, rule_detector, run_benchmark
from src.evaluation.qa_bench import main

DATASET = Path("docs/evals/datasets/injection_pairs.json")


def test_negation_is_removed_only_when_source_is_negated():
    case = injection.inject_negation(Pair("It does not work.", "它没有起作用。"))
    assert case.corrupted == "它起作用。"
    assert injection.inject_negation(Pair("It works.", "它没有问题。")) is None


def test_modality_is_strengthened_or_weakened():
    assert injection.inject_modality(Pair("It may help.", "它可能有帮助。")).corrupted == "它必然有帮助。"
    assert injection.inject_modality(Pair("You must stop.", "你必须停下。")).corrupted == "你可以停下。"


def test_number_swap_exchanges_two_distinct_numbers():
    case = injection.inject_number_swap(Pair("From 70 to 80.", "从 70 到 80。"))
    assert case.corrupted == "从 80 到 70。"
    assert injection.inject_number_swap(Pair("7 and 7", "7 和 7")) is None


def test_omission_drops_the_second_half_of_a_sentence():
    case = injection.inject_omission(Pair("A but B.", "该方法提高了准确率，但内存占用翻了一倍。"))
    assert case.corrupted == "该方法提高了准确率。"
    assert injection.inject_omission(Pair("# Title, here", "# 标题，这是一个很长的标题后半")) is None


def test_wrong_term_link_and_code_injections():
    glossary = {"War Machine": "战争机器 (War Machine)", "Numogram": "数字图 (Numogram)"}
    term = injection.inject_wrong_term(Pair("The War Machine runs.", "战争机器在运转。", glossary))
    assert term.corrupted == "数字图在运转。"
    link = injection.inject_link_target(Pair("[a](https://x.org/a)", "[甲](https://x.org/a)"))
    assert link.corrupted == "[甲](https://x.org/b)"
    code = injection.inject_code_content(Pair("`left = right + 1`", "`left = right + 1`"))
    assert code.corrupted == "`right = left + 1`"


def test_benchmark_counts_only_issues_new_to_the_corrupted_text():
    pairs = [Pair("It does not work.", "它没有起作用。"), Pair("Fine.", "很好。")]

    def detector(source, translation, glossary):
        found = {("noise", "总会报的误报")} if translation == "很好。" else set()
        if "没有" not in translation and "not" in source:
            found.add(("negation", "否定丢失"))
        return found

    report = run_benchmark(pairs, detector)
    assert report.false_positives == 1 and report.false_positive_rate == 0.5
    assert report.by_type["negation"].injected == 1 and report.by_type["negation"].recall == 1.0


def test_bundled_pairs_are_clean_and_cover_every_error_type():
    pairs, seeds = load_dataset(json.loads(DATASET.read_text(encoding="utf-8")))
    report = run_benchmark(pairs, rule_detector(), seed_cases=seeds)
    assert report.false_positives == 0  # 自带段对是正确译文，规则质检不应报问题
    assert all(stats.injected > 0 for stats in report.by_type.values())
    assert {seed["error_type"] for seed in report.seed_results} == {
        "negation",
        "modality",
        "number_swap",
        "omission",
        "code_content",
        "link_target",
    }


def test_records_are_split_into_paragraph_pairs():
    records = [
        {"success": True, "original": "First one.\n\nSecond one.", "translation": "第一。\n\n第二。"},
        {"success": True, "original": "Unaligned.\n\nTwo.", "translation": "只有一段。"},
        {"success": False, "original": "Failed.", "translation": "Failed."},
    ]
    pairs = injection.pairs_from_records(records)
    assert [(p.source, p.translation) for p in pairs] == [("First one.", "第一。"), ("Second one.", "第二。")]


def test_cli_runs_without_keys_and_writes_reports(tmp_path, capsys):
    assert main(["--out", str(tmp_path)]) == 0
    assert "召回率" in capsys.readouterr().out
    payload = json.loads((tmp_path / "qa_injection.json").read_text(encoding="utf-8"))
    assert set(payload["自带段对"]["by_type"]) == set(injection.ERROR_TYPES)
    assert (tmp_path / "qa_injection.md").exists()
    assert len(inject_all([Pair("It may not work.", "它可能不起作用。")])) == 2
