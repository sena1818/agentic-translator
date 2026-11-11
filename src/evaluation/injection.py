"""
注入错误基准：不依赖密钥和人工标注，度量质检器对各类已知错误的召回率与误报率。

做法：取正确的 (原文, 译文) 段对，用程序把译文改坏成某一类已知错误，再交给质检器；
质检器在改坏的译文上报出了原样译文上没有的问题，就算检出。原样段对上报出任何问题算误报。

七类注入（只在段对具备条件时注入，例如原文有否定词、译文里有否定字才做否定反转）：
- negation    反转否定：删掉译文里的否定词
- modality    改变情态强弱："可能"改"必然"、"必须"改"可以"
- number_swap 交换数字：译文里两个不同的数字互换位置
- omission    漏译半句：删掉译文后半的分句
- wrong_term  换错术语：术语表译名换成另一个条目的译名
- link_target 改链接地址：Markdown 链接的 URL 改一个字符
- code_content 改代码内容：代码里两个标识符互换

这里度量的是"检测器能否抓住特定已知错误"，不是真实译文的错误率；注入错误比真实错误更规整，
召回率是上限参考，不能当作真实场景的检出率。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

ERROR_TYPES = (
    "negation",
    "modality",
    "number_swap",
    "omission",
    "wrong_term",
    "link_target",
    "code_content",
)
ERROR_LABELS = {
    "negation": "反转否定",
    "modality": "改变情态强弱",
    "number_swap": "交换数字",
    "omission": "漏译半句",
    "wrong_term": "换错术语",
    "link_target": "改链接地址",
    "code_content": "改代码内容",
}


@dataclass(frozen=True)
class Pair:
    """一条正确的原文与译文段对。"""

    source: str
    translation: str
    glossary: dict[str, str] = field(default_factory=dict)
    origin: str = ""


@dataclass(frozen=True)
class InjectedCase:
    """一条被注入了已知错误的段对。"""

    error_type: str
    pair: Pair
    corrupted: str
    note: str = ""


# 检测器：给定 (原文, 译文, 术语表) 返回问题签名集合；签名相同视为同一个问题
Detector = Callable[[str, str, dict[str, str]], set[tuple[str, str]]]


# --------------------------------------------------------------------------- #
# 注入器
# --------------------------------------------------------------------------- #

_EN_NEGATION = re.compile(r"\b(?:not|never|no|none|nothing|nobody|neither|nor|cannot|without)\b|n't\b", re.I)
_ZH_NEGATION = re.compile(r"并不|并没有|没有|从未|从不|不再|不|没|未|无法|无")

_EN_WEAK = re.compile(r"\b(?:may|might|could|possibly|perhaps|likely|suggests?|appears?|seems?)\b", re.I)
_EN_STRONG = re.compile(r"\b(?:must|always|certainly|proves?|definitely|necessarily|shall)\b", re.I)
_ZH_WEAK_TO_STRONG = [
    ("可能", "必然"),
    ("或许", "必定"),
    ("也许", "一定"),
    ("似乎", "显然"),
    ("表明", "证明"),
    ("暗示", "证明"),
]
_ZH_STRONG_TO_WEAK = [
    ("必须", "可以"),
    ("必然", "可能"),
    ("一定", "也许"),
    ("总是", "有时"),
    ("证明", "暗示"),
    ("必定", "或许"),
]

_NUMBER = re.compile(r"(?<![0-9A-Za-z_.])\d+(?:\.\d+)?(?![0-9A-Za-z_])")
_CLAUSE_END = re.compile(r"(?<=[，；：])")
_CJK = re.compile(r"[一-鿿]")
_LINK = re.compile(r"\]\(([^)\s]+)")
_CODE = re.compile(r"```[^\n]*\n([\s\S]*?)```|`([^`\n]+)`")
_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z_0-9]*\b")
_CODE_KEYWORDS = {"def", "return", "if", "else", "for", "in", "import", "from", "class", "and", "or", "not", "None"}


def _preferred(zh: str) -> str:
    return zh.split(" (", 1)[0].strip()


def inject_negation(pair: Pair) -> InjectedCase | None:
    if not _EN_NEGATION.search(pair.source):
        return None
    match = _ZH_NEGATION.search(pair.translation)
    if not match:
        return None
    corrupted = pair.translation[: match.start()] + pair.translation[match.end() :]
    return InjectedCase("negation", pair, corrupted, f"删去「{match.group(0)}」")


def inject_modality(pair: Pair) -> InjectedCase | None:
    tables = []
    if _EN_WEAK.search(pair.source):
        tables.append(_ZH_WEAK_TO_STRONG)
    if _EN_STRONG.search(pair.source):
        tables.append(_ZH_STRONG_TO_WEAK)
    for table in tables:
        for weak, strong in table:
            if weak in pair.translation:
                return InjectedCase(
                    "modality", pair, pair.translation.replace(weak, strong, 1), f"「{weak}」改为「{strong}」"
                )
    return None


def inject_number_swap(pair: Pair) -> InjectedCase | None:
    matches = list(_NUMBER.finditer(pair.translation))
    first = matches[0] if matches else None
    second = next((m for m in matches[1:] if first and m.group(0) != first.group(0)), None)
    if not first or not second:
        return None
    text = pair.translation
    corrupted = (
        text[: first.start()]
        + second.group(0)
        + text[first.end() : second.start()]
        + first.group(0)
        + text[second.end() :]
    )
    return InjectedCase("number_swap", pair, corrupted, f"{first.group(0)} 与 {second.group(0)} 互换")


def inject_omission(pair: Pair) -> InjectedCase | None:
    """删掉译文后半的分句；被删部分至少 6 个汉字，避免只删掉一个虚词。"""
    text = pair.translation.strip()
    if "```" in text or "\n" in text or "{" in text or text.startswith(("#", "|", ">", "-", "*")):
        return None  # 只在单行正文段落上删分句，不切进标题、锚点属性、表格和列表
    clauses = [part for part in _CLAUSE_END.split(pair.translation.strip()) if part]
    if len(clauses) < 2:
        return None
    keep = clauses[: (len(clauses) + 1) // 2]
    dropped = "".join(clauses[len(keep) :])
    if len(_CJK.findall(dropped)) < 6:
        return None
    kept = "".join(keep).rstrip("，；：")
    ending = pair.translation.strip()[-1] if pair.translation.strip()[-1] in "。！？" else ""
    return InjectedCase("omission", pair, kept + ending, f"删去「{dropped[:20]}」")


def inject_wrong_term(pair: Pair) -> InjectedCase | None:
    renderings = [(en, _preferred(zh)) for en, zh in pair.glossary.items()]
    for en, zh in renderings:
        if not zh or zh not in pair.translation or en.lower() not in pair.source.lower():
            continue
        wrong = next((other for _, other in renderings if other and other != zh and other not in zh), None)
        if wrong:
            return InjectedCase("wrong_term", pair, pair.translation.replace(zh, wrong), f"「{zh}」换成「{wrong}」")
    return None


def inject_link_target(pair: Pair) -> InjectedCase | None:
    match = _LINK.search(pair.translation)
    if not match:
        return None
    url = match.group(1)
    last = url[-1]
    replacement = "b" if last != "b" else "c"
    corrupted = pair.translation[: match.start(1)] + url[:-1] + replacement + pair.translation[match.end(1) :]
    return InjectedCase("link_target", pair, corrupted, f"{url} → {url[:-1] + replacement}")


def inject_code_content(pair: Pair) -> InjectedCase | None:
    for match in _CODE.finditer(pair.translation):
        group = 1 if match.group(1) is not None else 2
        code = match.group(group)
        names = []
        for name in _IDENTIFIER.findall(code):
            if name not in _CODE_KEYWORDS and name not in names:
                names.append(name)
        if len(names) < 2:
            continue
        a, b = names[0], names[1]
        swapped = re.sub(
            rf"\b(?:{re.escape(a)}|{re.escape(b)})\b", lambda m, a=a, b=b: b if m.group(0) == a else a, code
        )
        corrupted = pair.translation[: match.start(group)] + swapped + pair.translation[match.end(group) :]
        return InjectedCase("code_content", pair, corrupted, f"{a} 与 {b} 互换")
    return None


INJECTORS: dict[str, Callable[[Pair], InjectedCase | None]] = {
    "negation": inject_negation,
    "modality": inject_modality,
    "number_swap": inject_number_swap,
    "omission": inject_omission,
    "wrong_term": inject_wrong_term,
    "link_target": inject_link_target,
    "code_content": inject_code_content,
}


def inject_all(pairs: Iterable[Pair]) -> list[InjectedCase]:
    """对每个段对尝试全部七类注入，返回能注入的那些。"""
    cases = []
    for pair in pairs:
        for injector in INJECTORS.values():
            case = injector(pair)
            if case is not None and case.corrupted != pair.translation:
                cases.append(case)
    return cases


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #


@dataclass
class TypeRecall:
    error_type: str
    injected: int
    detected: int
    missed_examples: list[dict] = field(default_factory=list)

    @property
    def recall(self) -> float | None:
        return round(self.detected / self.injected, 4) if self.injected else None


@dataclass
class BenchmarkReport:
    detector: str
    clean_pairs: int
    false_positives: int
    by_type: dict[str, TypeRecall]
    seed_results: list[dict] = field(default_factory=list)
    false_positive_examples: list[dict] = field(default_factory=list)

    @property
    def false_positive_rate(self) -> float | None:
        return round(self.false_positives / self.clean_pairs, 4) if self.clean_pairs else None

    @property
    def overall_recall(self) -> float | None:
        injected = sum(t.injected for t in self.by_type.values())
        detected = sum(t.detected for t in self.by_type.values())
        return round(detected / injected, 4) if injected else None

    def as_dict(self) -> dict:
        return {
            "detector": self.detector,
            "clean_pairs": self.clean_pairs,
            "false_positives": self.false_positives,
            "false_positive_rate": self.false_positive_rate,
            "overall_recall": self.overall_recall,
            "by_type": {
                name: {
                    "injected": t.injected,
                    "detected": t.detected,
                    "recall": t.recall,
                    "missed_examples": t.missed_examples,
                }
                for name, t in self.by_type.items()
            },
            "seed_results": self.seed_results,
            "false_positive_examples": self.false_positive_examples,
        }


def run_benchmark(
    pairs: list[Pair],
    detector: Detector,
    *,
    detector_name: str = "rules",
    seed_cases: list[InjectedCase] | None = None,
    max_examples: int = 3,
) -> BenchmarkReport:
    """在原样段对上统计误报，在注入段对上按类型统计召回。

    检出口径：改坏后的译文报出了原样译文上没有的问题签名。只比较新增问题，
    避免把原样段对上本就存在的误报算成"检出"。
    """
    baseline: dict[int, set[tuple[str, str]]] = {}
    false_positives = 0
    fp_examples: list[dict] = []
    for pair in pairs:
        found = detector(pair.source, pair.translation, pair.glossary)
        baseline[id(pair)] = found
        if found:
            false_positives += 1
            if len(fp_examples) < max_examples * 3:
                fp_examples.append({"source": pair.source[:120], "issues": sorted(m for _, m in found)})

    by_type = {name: TypeRecall(name, 0, 0) for name in ERROR_TYPES}
    for case in inject_all(pairs):
        clean = baseline[id(case.pair)]
        new = detector(case.pair.source, case.corrupted, case.pair.glossary) - clean
        stats = by_type[case.error_type]
        stats.injected += 1
        if new:
            stats.detected += 1
        elif len(stats.missed_examples) < max_examples:
            stats.missed_examples.append({"source": case.pair.source[:120], "change": case.note})

    seed_results = []
    for case in seed_cases or []:
        clean = detector(case.pair.source, case.pair.translation, case.pair.glossary)
        new = detector(case.pair.source, case.corrupted, case.pair.glossary) - clean
        seed_results.append(
            {
                "error_type": case.error_type,
                "source": case.pair.source,
                "corrupted": case.corrupted,
                "detected": bool(new),
                "issues": sorted(message for _, message in new),
            }
        )

    return BenchmarkReport(
        detector=detector_name,
        clean_pairs=len(pairs),
        false_positives=false_positives,
        by_type=by_type,
        seed_results=seed_results,
        false_positive_examples=fp_examples,
    )


# --------------------------------------------------------------------------- #
# 段对来源
# --------------------------------------------------------------------------- #

_PARAGRAPH = re.compile(r"\n\s*\n")


def pairs_from_records(records: list[dict], glossary: dict[str, str] | None = None) -> list[Pair]:
    """从结构化翻译的 records.json 取段对：成功的块按空行拆段，段数相同才一一配对。

    仅用于在本地真实译文上统计误报；真实译文本身未经人工核对，误报数字是"质检器在
    未注入的译文上报了多少问题"，其中可能有真问题。
    """
    pairs = []
    for record in records:
        if not record.get("success"):
            continue
        sources = [p for p in _PARAGRAPH.split(record.get("original", "").strip()) if p.strip()]
        targets = [p for p in _PARAGRAPH.split(record.get("translation", "").strip()) if p.strip()]
        if len(sources) != len(targets):
            continue
        for source, target in zip(sources, targets, strict=True):
            if _CJK.search(target) and re.search(r"[A-Za-z]{3,}", source):
                pairs.append(Pair(source, target, dict(glossary or {}), origin=str(record.get("chunk_id", ""))))
    return pairs


def load_dataset(data: dict) -> tuple[list[Pair], list[InjectedCase]]:
    """读取 injection_pairs.json：{"glossary": {...}, "pairs": [...], "seed_cases": [...]}。"""
    glossary = data.get("glossary", {})
    pairs = [
        Pair(item["source"], item["translation"], {**glossary, **item.get("glossary", {})}, item.get("origin", ""))
        for item in data.get("pairs", [])
    ]
    seeds = [
        InjectedCase(
            item["error_type"],
            Pair(item["source"], item["translation"], {**glossary, **item.get("glossary", {})}, "seed"),
            item["corrupted"],
            item.get("note", ""),
        )
        for item in data.get("seed_cases", [])
    ]
    return pairs, seeds


def rule_detector(validator=None) -> Detector:
    """当前规则质检（TranslationValidator）作为检测器。"""
    from ..core.validator import TranslationValidator

    validator = validator or TranslationValidator()

    def detect(source: str, translation: str, glossary: dict[str, str]) -> set[tuple[str, str]]:
        return {(issue.kind, issue.message) for issue in validator.validate(source, translation, glossary).issues}

    return detect


def audit_detector() -> Detector:
    """全书非文字审计（#19）作为检测器：把段对解析成 AST 后比对图片、公式、代码、表格、脚注、链接与编号引用。

    只计翻译引入的问题；需要 pandoc。
    """
    import functools
    import json

    from ..domain.document.nontext_audit import AuditUnit, audit_document
    from ..infrastructure.document import pandoc

    @functools.lru_cache(maxsize=4096)
    def parse(text: str) -> str:
        return json.dumps(pandoc.markdown_to_ast(text)["blocks"])

    def detect(source: str, translation: str, glossary: dict[str, str]) -> set[tuple[str, str]]:
        source_blocks, target_blocks = json.loads(parse(source)), json.loads(parse(translation))
        report = audit_document([AuditUnit(source_blocks, target_blocks)], source_blocks, target_blocks)
        return {(f"audit:{f.kind}", f.message) for f in report.by_origin("translation")}

    return detect


def combined_detector(*detectors: Detector) -> Detector:
    def detect(source: str, translation: str, glossary: dict[str, str]) -> set[tuple[str, str]]:
        found: set[tuple[str, str]] = set()
        for detector in detectors:
            found |= detector(source, translation, glossary)
        return found

    return detect
