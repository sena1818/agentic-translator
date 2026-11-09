"""
术语表按块筛选

只把当前块（正文 + 上文语境）中实际出现的术语注入 prompt 与缓存键，
避免整表注入稀释模型注意力、浪费 token，也让「向术语表新增无关术语」
不再使全书缓存失效。匹配口径与 validator._check_glossary 保持一致，见 term_occurs。
"""
from __future__ import annotations

import re


def term_occurs(term: str, text: str, respect_case: bool = False) -> bool:
    """术语是否在文本中作为术语出现。

    - 整词匹配（允许英文复数 s/es），避免 `Sun` 命中 `Sunday`、`Oil` 命中 `toil`；
    - 默认大小写无关（论文里 "backpropagation" 小写也是术语）；
    - respect_case=True 时（哲学/理论著作，概念靠首字母大写标记），首字母大写的术语
      只在文中以大写形式出现才算命中：`Earth`（地球）不约束 "openings in the earth"，
      `Solid`（固态）不约束 "the ground was never solid"。
    """
    if not term or not text:
        return False
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?:e?s)?(?![A-Za-z0-9])", re.IGNORECASE)
    capitalized = respect_case and term[0].isupper()
    for match in pattern.finditer(text):
        if not capitalized or not match.group(0)[0].islower():
            return True
    return False


def select_relevant_glossary(
    glossary: dict[str, str],
    *texts: str,
    respect_case: bool = False,
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
    haystack = "\n".join(text for text in texts if text)
    if not haystack:
        return {}
    return {en: zh for en, zh in glossary.items() if term_occurs(en, haystack, respect_case)}


def respect_case(config) -> bool:
    """从配置读取术语大小写口径；配置缺失（测试替身）时按大小写无关处理。"""
    getter = getattr(config, "get", None)
    return bool(getter("quality.glossary_respect_case", False)) if getter else False
