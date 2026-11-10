"""
术语表按块筛选

只把当前块（正文 + 上文语境）中实际出现的术语注入 prompt 与缓存键，
避免整表注入稀释模型注意力、浪费 token，也让「向术语表新增无关术语」
不再使全书缓存失效。

匹配口径由 ``term_appears_in`` 统一定义，``select_relevant_glossary`` 与
``validator.TranslationValidator._check_glossary`` 都必须调用它，保证二者一致：

- 大小写无关；
- 按单词边界匹配：术语前后不能紧邻 ASCII 字母或数字，
  因此 ``AI`` 不会命中 ``said``，``Oil`` 不会命中 ``boiling``；
- 兼容常见复数与所有格后缀：``-s``、``-es``、``'s``、``s'``
  （含弯引号 ``’``），以及辅音 + ``y`` 结尾的 ``-ies``（``ontology`` → ``ontologies``）；
- 多词术语中的空格、连字符可互换且可为任意长度的空白 / 连字符序列，
  因此 ``War Machine`` 能命中 ``war-machine``、``war\\nmachine``。
"""
from __future__ import annotations

import re
from functools import lru_cache

_LEFT_BOUNDARY = r"(?<![A-Za-z0-9])"
_RIGHT_BOUNDARY = r"(?![A-Za-z0-9])"
# 顺序无关紧要（后面紧跟边界断言，正则会自行回溯），这里按长度排列便于阅读
_INFLECTION_SUFFIX = r"(?:['’]s|s['’]|es|s)?"
_SEPARATOR = r"[\s\-]+"
_TOKEN_SPLIT = re.compile(r"[\s\-]+")
_CONSONANT_Y = re.compile(r"[b-df-hj-np-tv-z]y$", re.IGNORECASE)


@lru_cache(maxsize=4096)
def compile_term_pattern(term: str) -> re.Pattern[str] | None:
    """把英文术语编译成匹配正则；空术语返回 None。"""
    tokens = [token for token in _TOKEN_SPLIT.split(term.strip()) if token]
    if not tokens:
        return None

    escaped = [re.escape(token) for token in tokens]
    last = tokens[-1]
    if _CONSONANT_Y.search(last):
        # ontology -> ontolog(?:y|ies)
        escaped[-1] = re.escape(last[:-1]) + r"(?:y|ies)"

    body = _SEPARATOR.join(escaped)
    return re.compile(
        _LEFT_BOUNDARY + body + _INFLECTION_SUFFIX + _RIGHT_BOUNDARY,
        re.IGNORECASE,
    )


def term_appears_in(term: str, text: str) -> bool:
    """判断英文术语是否出现在 text 中（口径见模块文档字符串）。"""
    if not text:
        return False
    pattern = compile_term_pattern(term)
    return pattern is not None and pattern.search(text) is not None


def select_relevant_glossary(
    glossary: dict[str, str],
    *texts: str,
) -> dict[str, str]:
    """返回 glossary 中英文术语出现在任一 text 内的子集，保持原插入顺序。

    Args:
        glossary: 完整术语表（英文术语 -> 中文译名）
        texts: 参与匹配的文本片段（如块正文、上文语境）

    Returns:
        过滤后的术语表；无术语或无文本时返回空 dict。
    """
    if not glossary:
        return {}
    # 逐段匹配，避免多词术语跨越「上文语境 / 正文」的拼接处被误命中
    haystacks = [text for text in texts if text]
    if not haystacks:
        return {}
    return {
        en: zh
        for en, zh in glossary.items()
        if any(term_appears_in(en, text) for text in haystacks)
    }
