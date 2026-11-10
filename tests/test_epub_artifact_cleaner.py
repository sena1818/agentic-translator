from src.pipelines.postprocess.markdown_formatter import SmartMarkdownFormatter
from src.pipelines.preprocess.artifact_cleaner import EpubArtifactCleaner


def test_cleaner_removes_pagebreaks_and_normalizes_spans_and_images():
    cleaner = EpubArtifactCleaner()
    content = """::: {#index_split_002.html_calibre_pb_2 .mbp_pagebreak}
:::

[第一篇章]{.calibre2}
[同一（性）]{.calibre2}

![](data/temp/task/images/images/00005.jpg)
{.calibre_2}

[]{#index_split_003.html}
"""

    cleaned = cleaner.clean(content)

    assert "mbp_pagebreak" not in cleaned
    assert "[]{#index_split_003.html}" not in cleaned
    assert "## 第一篇章" in cleaned
    assert "## 同一（性）" in cleaned
    assert "![00005](images/00005.jpg)" in cleaned


def test_cleaner_handles_bilingual_quote_artifacts():
    cleaner = EpubArtifactCleaner()
    content = r"""> ::: {#index_split_002.html_calibre_pb_2 .mbp_pagebreak}
> :::
>
> [Foreword]
>
> [ ]
>
> ![](data/temp/task/images/images/00005.jpg)
> {.calibre_2}
>
> 1\) First item
>
> <svg xmlns="http://www.w3.org/2000/svg">
> `<image width="801" height="1186" xlink:href="images/calibre_cover.jpg">`{=html}`</image>`{=html}
> </svg>
"""

    cleaned = cleaner.clean(content)

    assert "mbp_pagebreak" not in cleaned
    assert "[ ]" not in cleaned
    assert "<svg" not in cleaned
    assert "> Foreword" in cleaned
    assert "> ![00005](images/00005.jpg)" in cleaned
    assert "> 1) First item" in cleaned


def test_formatter_cleans_epub_residue_end_to_end():
    formatter = SmartMarkdownFormatter()
    content = """::: {#index_split_002.html_calibre_pb_2 .mbp_pagebreak}
:::

[第一篇章]{.calibre2}

![](data/temp/task/images/images/00005.jpg)
{.calibre_2}
"""

    formatted = formatter.format(content)

    assert "mbp_pagebreak" not in formatted
    assert "## 第一篇章" in formatted
    assert "![00005](images/00005.jpg)" in formatted


def test_cleaner_supports_source_specific_rules():
    cleaner = EpubArtifactCleaner()
    content = """[]{#kindlepos123}
[]{#filepos456}
[Chapter One]{.calibre3}
"""

    cleaned = cleaner.clean(content, source_type="kindle")

    assert "kindlepos" not in cleaned
    assert "filepos" not in cleaned
    assert "### Chapter One" in cleaned


def test_cleaner_unwraps_nested_calibre_spans():
    cleaner = EpubArtifactCleaner()
    content = r"""[[NICK]{.calibre_1}]{.calibre1}[ ]{.calibre_1}[[LAND]{.calibre_1}]{.calibre1}
[[*]{.calibre_9}]{.calibre2}
[[[Edited By]{.calibre_1}]{.italic}]{.calibre2}
[[Fanged Noumena]{.calibre_1}]{.calibre3}
"""

    cleaned = cleaner.clean(content, source_type="epub")

    assert "NICK LAND" in cleaned
    assert "[[*]{.calibre_9}]{.calibre2}" not in cleaned
    assert "\n*\n" not in f"\n{cleaned}\n"
    assert "## *Edited By*" in cleaned
    assert "### Fanged Noumena" in cleaned


def test_cleaner_unwraps_long_calibre_line_with_nested_footnote():
    cleaner = EpubArtifactCleaner()
    content = (
        "> [Following Deleuze,^[[3](#index_split_004.html_filepos114207)]{.small}^ "
        "Land refuses the marginalizing of aesthetics.]{.calibre_1}\n"
        "> > > >\n"
    )

    cleaned = cleaner.clean(content, source_type="epub")

    assert (
        "> Following Deleuze,^[3](#index_split_004.html_filepos114207)^ "
        "Land refuses the marginalizing of aesthetics."
    ) in cleaned
    assert "]{.calibre_1}" not in cleaned
    assert "> > > >" not in cleaned


def test_cleaner_strips_inline_class_suffixes():
    cleaner = EpubArtifactCleaner()
    content = (
        "第一部 分]{.calibre6}：]{.calibre6}狼群\n"
        "这种批判的情动再物质化将追问重构为]{.calibre_1}*勘探*\n"
    )

    cleaned = cleaner.clean(content, source_type="epub")

    assert "第一部 分：狼群" in cleaned
    assert "这种批判的情动再物质化将追问重构为*勘探*" in cleaned
    assert "]{.calibre6}" not in cleaned


def test_svg_wrapper_removal_scales_to_long_documents():
    # pandoc 3.x 把封面 SVG 包成 `::: {}`；其后若有数十行正文，旧正则会指数级回溯
    cleaner = EpubArtifactCleaner()
    body = "".join(f"Paragraph {i} of the book.\n\n" for i in range(200))
    content = (
        "::: {}\n"
        '<svg xmlns="http://www.w3.org/2000/svg">\n'
        '`<image width="801" xlink:href="images/cover.jpg">`{=html}`</image>`{=html}\n'
        "</svg>\n"
        ":::\n\n"
        + body
        + "::: {}\n"
        "Plain wrapped text.\n"
        ":::\n"
    )

    cleaned = cleaner._remove_svg_wrapper_blocks(content)

    assert "<svg" not in cleaned and "<image" not in cleaned
    assert cleaned.startswith("\nParagraph 0 of the book.")
    assert "::: {}\nPlain wrapped text.\n:::\n" in cleaned
    assert "Paragraph 199 of the book." in cleaned


def test_svg_wrapper_removal_handles_quoted_and_unclosed_blocks():
    cleaner = EpubArtifactCleaner()
    quoted = "> ::: {}\n> <svg>\n> </svg>\n> :::\n> After\n"
    unclosed = "::: {}\n<svg>\nno closing fence\n"

    assert cleaner._remove_svg_wrapper_blocks(quoted) == "> After\n"
    assert cleaner._remove_svg_wrapper_blocks(unclosed) == unclosed
