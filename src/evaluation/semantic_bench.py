"""
语义质检在注入错误基准上的检测器与成本估算。

- semantic_detector：把 SemanticChecker（启发式，可选模型）包装成基准用的检测器；
- combine：多个检测器的问题取并集（"规则 + 语义"）；
- routing_report：只查高风险段时，模型要看多少段、估算多少 token、注入错误有多少落在高风险段
  （后者是"只查高风险段"时模型召回率的上限）。
"""

from __future__ import annotations

import asyncio

from ..domain.rules.semantic_signals import is_high_risk
from ..pipelines.translate.semantic_checker import SegmentPair, SemanticChecker, build_prompt, estimate_tokens
from .injection import ERROR_TYPES, Detector, Pair, inject_all


def semantic_detector(checker: SemanticChecker | None = None) -> Detector:
    checker = checker or SemanticChecker()

    def detect(source: str, translation: str, glossary: dict[str, str]) -> set[tuple[str, str]]:
        result = asyncio.run(checker.check([SegmentPair(0, source, translation)]))
        return {("semantic", finding.message()) for finding in result.findings.get(0, [])}

    return detect


def combine(*detectors: Detector) -> Detector:
    def detect(source: str, translation: str, glossary: dict[str, str]) -> set[tuple[str, str]]:
        found: set[tuple[str, str]] = set()
        for detector in detectors:
            found |= detector(source, translation, glossary)
        return found

    return detect


def _prompt_tokens(pairs: list[Pair], batch_size: int) -> int:
    segments = [SegmentPair(i, pair.source, pair.translation) for i, pair in enumerate(pairs)]
    return sum(
        estimate_tokens(build_prompt(segments[start : start + batch_size]))
        for start in range(0, len(segments), batch_size)
    )


def routing_report(pairs: list[Pair], batch_size: int = 12) -> dict:
    """模型语义检查的成本估算与"只查高风险段"的召回上限。token 为按字符粗估，未调用模型。"""
    risky = [pair for pair in pairs if is_high_risk(pair.source)]
    risky_ids = {id(pair) for pair in risky}
    translation_tokens = sum(estimate_tokens(pair.source) + estimate_tokens(pair.translation) for pair in pairs)
    by_type = {}
    for error_type in ERROR_TYPES:
        cases = [case for case in inject_all(pairs) if case.error_type == error_type]
        in_risky = sum(1 for case in cases if id(case.pair) in risky_ids)
        by_type[error_type] = {
            "injected": len(cases),
            "in_high_risk": in_risky,
            "recall_ceiling": round(in_risky / len(cases), 4) if cases else None,
        }
    all_tokens = _prompt_tokens(pairs, batch_size)
    risky_tokens = _prompt_tokens(risky, batch_size)
    return {
        "segments": len(pairs),
        "high_risk_segments": len(risky),
        "estimated_prompt_tokens_all": all_tokens,
        "estimated_prompt_tokens_high_risk": risky_tokens,
        "source_plus_translation_tokens": translation_tokens,
        "by_type": by_type,
    }
