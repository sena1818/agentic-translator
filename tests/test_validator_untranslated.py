"""未翻译英文残留检测的回归用例。"""

from src.core.validator import TranslationValidator

ENGLISH = (
    "The war machine, as Deleuze argues, is exterior to the state apparatus, "
    "and it operates through speed, secrecy, and nomadic distribution of space."
)


def _kinds(translation: str, original: str = ENGLISH, validator: TranslationValidator | None = None) -> set[str]:
    validator = validator or TranslationValidator()
    return {issue.kind for issue in validator.validate(original, translation, {}).issues}


def test_fully_untranslated_paragraph_with_punctuation_is_flagged():
    assert "untranslated" in _kinds(ENGLISH)


def test_half_translated_chunk_is_flagged():
    original = ENGLISH + " " + ENGLISH
    assert "untranslated" in _kinds("战争机器外在于国家机器。" + ENGLISH, original)


def test_untranslated_sentence_with_fullwidth_punctuation_is_flagged():
    text = ENGLISH.replace(", ", "，").replace(".", "。")
    assert "untranslated" in _kinds(text)


def test_english_interleaved_with_short_chinese_glosses_is_flagged():
    text = (
        "The war machine (战争机器), as Deleuze argues, is exterior to the state apparatus (国家机器), "
        "and it operates through speed, secrecy, and nomadic distribution of space."
    )
    assert "untranslated" in _kinds(text)


def test_original_sentence_echoed_inside_parentheses_is_flagged():
    text = "战争机器外在于国家机器，它通过速度、隐秘与游牧式的空间分配运作。(" + ENGLISH + ")"
    assert "untranslated" in _kinds(text)


def test_translation_with_glossary_and_name_annotations_passes():
    original = (
        "Hyperstition, as Nick Land and the CCRU describe it in Cyclonopedia, is a fiction "
        "that makes itself real; Gilles Deleuze and Félix Guattari call it the war machine."
    )
    translation = (
        "正如尼克·兰德 (Nick Land) 与控制论文化研究小组 (Cybernetic Culture Research Unit) 在"
        "《Cyclonopedia: Complicity with Anonymous Materials》中所述，超虚构 (Hyperstition) "
        "是一种使自身成真的虚构；吉尔·德勒兹（Gilles Deleuze）与费利克斯·瓜塔里（Félix Guattari）"
        "称之为战争机器 (War Machine)。参见 Mark Fisher 的 Capitalist Realism。"
    )
    assert "untranslated" not in _kinds(translation, original)


def test_english_inside_code_blocks_inline_code_math_and_urls_passes():
    translation = (
        "下面的代码展示了训练循环：\n\n"
        "```python\n"
        "# compute the loss for every batch and then update the model weights with the optimizer\n"
        "for batch in loader: loss = model(batch).loss; loss.backward(); optimizer.step()\n"
        "```\n\n"
        "调用 `model.fit(x, y, epochs=10, batch_size=32, validation_split=0.2, shuffle=True, verbose=1)` 即可。"
        "损失函数为 $\\mathcal{L} = \\sum_{i} \\log p(y_i \\mid x_i, \\theta) + \\lambda \\lVert \\theta \\rVert$。"
        "详见 https://example.com/docs/how-to-train-a-model-with-the-default-settings-and-the-new-api 。"
    )
    original = translation  # 只关心未翻译检测，避免数字/公式保真规则干扰
    assert "untranslated" not in _kinds(translation, original)


def test_identifiers_and_file_names_in_tables_are_not_flagged():
    table = (
        "| 用途 | 推荐工具 |\n|------|---------|\n"
        "| 学术出版 | smart_markdown_formatter |\n| 个人阅读 | advanced_markdown_cleaner |\n"
        "| 网页发布 | smart_markdown_formatter |\n| 纯文本存档 | advanced_markdown_cleaner |\n"
        "\n运行 scripts/smart_markdown_formatter.py 处理 data/output/your_book_CN.md 即可。"
    )
    assert "untranslated" not in _kinds(table, table)


def test_reference_list_entry_is_not_flagged():
    entry = (
        "Deleuze, G., & Guattari, F. (1987). A Thousand Plateaus: Capitalism and Schizophrenia. "
        "Minneapolis: University of Minnesota Press."
    )
    assert "untranslated" not in _kinds(entry, entry)


def test_normal_chinese_translation_passes():
    translation = "正如德勒兹所言，战争机器外在于国家机器，它通过速度、隐秘以及游牧式的空间分配来运作。"
    assert "untranslated" not in _kinds(translation)


def test_word_span_and_ratio_remain_configurable():
    short = "the war machine is outside the state."
    assert "untranslated" in _kinds(short, short, TranslationValidator(untranslated_word_span=4))
    assert "untranslated" not in _kinds(short, short, TranslationValidator(untranslated_word_span=12))

    # 中文直接嵌在英文里，把英文切成短片段，只有段落级比例信号能检出。
    glossed = "The war machine 战争机器 is exterior to the state apparatus 国家机器 and it operates through secrecy."
    without_ratio = TranslationValidator(untranslated_word_span=12, untranslated_latin_ratio=0)
    assert "untranslated" not in _kinds(glossed, glossed, without_ratio)
    assert "untranslated" in _kinds(glossed, glossed, TranslationValidator(untranslated_word_span=12))
