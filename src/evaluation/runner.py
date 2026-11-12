"""
评测编排：对三组对照的每个变体跑真实翻译，交裁判打分，汇总成报告。

真实翻译由既有 TranslationEngine 驱动（注入不同术语表 + 临时缓存 + 配置覆盖），
token 用量经 UsageMetadataCollector 采集。这部分需要选手与裁判的真实密钥；
cli 的 --dry-run 会换上假模型工厂与假裁判，机械跑通同一条流水线用于冒烟。
"""
from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.translator import TranslationEngine
from ..domain.models.translation_models import TranslationResult
from ..infrastructure.cache.translation_cache import TranslationCache
from ..infrastructure.observability import UsageMetadataCollector
from .aggregation import aggregate_variant, build_comparison
from .consistency import AlignedSegment, compute_consistency
from .judge import TranslationJudge
from .models import EvalSample, VariantRun
from .report import CostSummary, EvalReport


@dataclass(frozen=True)
class Variant:
    """一个对照变体：在基线配置上只拨动一个开关。"""

    key: str
    label: str
    multi_agent: bool = True
    glossary_enabled: bool = True


@dataclass(frozen=True)
class ComparisonSpec:
    name: str
    title: str
    variants: list[Variant]


def default_comparisons() -> list[ComparisonSpec]:
    """两组对照（基线：多 Agent 开 + 术语表有）。编排引擎对照随 native 引擎删除而移除（ADR-0001）。"""
    return [
        ComparisonSpec(
            "multi_agent",
            "多 Agent 协作：开 vs 关",
            [
                Variant("multi_on", "多 Agent 开", multi_agent=True),
                Variant("multi_off", "多 Agent 关", multi_agent=False),
            ],
        ),
        ComparisonSpec(
            "glossary",
            "术语表：有 vs 无",
            [
                Variant("glossary_on", "术语表有", glossary_enabled=True),
                Variant("glossary_off", "术语表无", glossary_enabled=False),
            ],
        ),
    ]


# 一致率分析取"生产基线"配置的产出：多 Agent 开 + 术语表有
REFERENCE_VARIANT = Variant("reference", "基线", multi_agent=True, glossary_enabled=True)


def _set_nested(mapping: dict, dotted_key: str, value: Any) -> None:
    keys = dotted_key.split(".")
    node = mapping
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = value


@contextmanager
def override_config(config, overrides: dict[str, Any]):
    """临时覆盖配置里的若干键，退出时精确还原（含原本不存在的键）。"""
    _MISSING = object()
    previous: dict[str, Any] = {}
    for dotted_key in overrides:
        node = config.config
        found = True
        for key in dotted_key.split("."):
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                found = False
                break
        previous[dotted_key] = node if found else _MISSING

    for dotted_key, value in overrides.items():
        _set_nested(config.config, dotted_key, value)
    try:
        yield
    finally:
        for dotted_key, old in previous.items():
            if old is _MISSING:
                # 删除本次新建的键
                keys = dotted_key.split(".")
                node = config.config
                for key in keys[:-1]:
                    node = node.get(key, {})
                node.pop(keys[-1], None)
            else:
                _set_nested(config.config, dotted_key, old)


class InstrumentedModelFactory:
    """包装真实/假模型工厂，给每个模型挂上 token 采集回调。"""

    def __init__(self, base_factory, collector: UsageMetadataCollector):
        self.base_factory = base_factory
        self.collector = collector

    def _wrap(self, model):
        return model.with_config({"callbacks": [self.collector]})

    def create_translator(self):
        return self._wrap(self.base_factory.create_translator())

    def create_checker(self):
        return self._wrap(self.base_factory.create_checker())

    def create_analyst(self):
        return self._wrap(self.base_factory.create_analyst())


@dataclass
class EvaluationRunner:
    """驱动三组对照评测。"""

    config: Any
    judge: TranslationJudge
    factory_builder: Callable[[UsageMetadataCollector], Any]
    samples: list[EvalSample]
    threshold: float = 0.90
    judge_usage: UsageMetadataCollector | None = None
    meta: dict = field(default_factory=dict)
    # 一致率分析的语料：默认取评测数据集的基线产出；短样本只有单块、跨块重复为 0，
    # 指向一篇较长英文源文才能真正算出术语外重复短语的一致率（见 methodology.md）。
    consistency_source: str | None = None
    consistency_glossary: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        self._reference_segments: list[AlignedSegment] = []
        self._total_tokens = 0
        self._total_calls = 0

    async def _run_variant_sample(
        self, variant: Variant, sample: EvalSample
    ) -> tuple[VariantRun, list[TranslationResult]]:
        glossary = sample.glossary if variant.glossary_enabled else {}
        return await self._run_variant_text(variant, sample.id, sample.source_text, glossary)

    async def _run_variant_text(
        self, variant: Variant, sample_id: str, source_text: str, glossary: dict[str, str]
    ) -> tuple[VariantRun, list[TranslationResult]]:
        overrides = {
            "multi_agent.enabled": variant.multi_agent,
            # 多 Agent 关：同时关掉审校员，退化为单翻译员
            "quality.enable_qa_check": variant.multi_agent,
        }
        glossary = dict(glossary)
        collector = UsageMetadataCollector()

        with override_config(self.config, overrides), tempfile.TemporaryDirectory() as tmp:
            factory = self.factory_builder(collector)
            cache = TranslationCache(Path(tmp) / "cache.db")
            engine = TranslationEngine(
                glossary=glossary,
                model_factory=factory,
                cache=cache,
            )
            output_path = Path(tmp) / "out.md"
            output_path.write_text("", encoding="utf-8")
            start = time.perf_counter()
            results = await engine.translate_batch(
                text=source_text,
                output_path=output_path,
            )
            latency = time.perf_counter() - start
            translation = output_path.read_text(encoding="utf-8")

        snapshot = collector.snapshot
        self._total_tokens += snapshot.total_tokens
        self._total_calls += snapshot.call_count
        run = VariantRun(
            variant=variant.key,
            sample_id=sample_id,
            translation=translation,
            total_tokens=snapshot.total_tokens,
            latency_s=round(latency, 3),
            call_count=snapshot.call_count,
        )
        return run, results

    async def _ensure_reference_segments(self) -> None:
        """用基线配置跑一遍，收集按块对齐的 (原文, 译文) 供一致率分析。"""
        if self._reference_segments:
            return
        if self.consistency_source is not None:
            _run, results = await self._run_variant_text(
                REFERENCE_VARIANT, "consistency-doc", self.consistency_source, self.consistency_glossary
            )
            self._collect_segments(results)
            return
        for sample in self.samples:
            _run, results = await self._run_variant_sample(REFERENCE_VARIANT, sample)
            self._collect_segments(results)

    def _collect_segments(self, results: list[TranslationResult]) -> None:
        for result in results:
            self._reference_segments.append(
                AlignedSegment(source=result.original, translation=result.translation)
            )

    def _consistency_glossary(self) -> dict[str, str]:
        if self.consistency_source is not None:
            return self.consistency_glossary
        return self.samples[0].glossary if self.samples else {}

    async def run(self) -> EvalReport:
        comparisons = []
        for spec in default_comparisons():
            aggregates = []
            for variant in spec.variants:
                runs, verdicts = [], []
                for sample in self.samples:
                    run, _results = await self._run_variant_sample(variant, sample)
                    verdict = await self.judge.score(sample, run.translation)
                    runs.append(run)
                    verdicts.append(verdict)
                aggregates.append(aggregate_variant(variant.key, variant.label, runs, verdicts))
            comparisons.append(build_comparison(spec.name, spec.title, aggregates))

        await self._ensure_reference_segments()
        consistency = compute_consistency(
            self._reference_segments,
            self._consistency_glossary(),
            threshold=self.threshold,
        )

        judge_tokens = self.judge_usage.snapshot.total_tokens if self.judge_usage else 0
        judge_calls = self.judge_usage.snapshot.call_count if self.judge_usage else 0
        cost = CostSummary(
            total_tokens=self._total_tokens + judge_tokens,
            total_calls=self._total_calls + judge_calls,
            estimated_cost_rmb=self.meta.get("estimated_cost_rmb"),
        )
        return EvalReport(comparisons=comparisons, consistency=consistency, cost=cost, meta=self.meta)
