"""
导出前的全书非文字审计：把回填好的文档拆成"原文块 ↔ 译文块"组，交给确定性审计规则。

审计结果：
- 写成 `{stem}.audit.json`；
- 翻译引入、带段号的问题并入对应段的问题列表，进入对照阅读器的问题导航；
- 摘要计数并入结构统计（`structure.json` → 任务质检摘要）。
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path

from ...domain.document import document_ir
from ...domain.document.nontext_audit import AuditReport, AuditUnit, audit_document

_ORIGIN_PREFIX = {"translation": "非文字审计", "source": "原文问题"}


def build_units(document) -> list[AuditUnit]:
    """已对齐的块逐单元比对（定位到单元的第一个段）；结构被改动的块整块比对；失败块只查原文。"""
    units: list[AuditUnit] = []
    for item in document.chunks:
        structured = item.structured
        first = structured.segments[0].seg_id if structured.segments else None
        if item.status == "failed":
            units.append(AuditUnit(structured.blocks, structured.blocks, first, translated=False))
        elif item.status == "unaligned":
            units.append(AuditUnit(structured.blocks, item.translated_blocks, first))
        else:
            offset = 0
            for unit, translated in zip(structured.units, item.translated_blocks, strict=True):
                count = sum(1 for _ in document_ir.iter_candidates([unit.block]))
                seg = next(
                    (s.seg_id for s in structured.segments if offset <= s.candidate_index < offset + count), first
                )
                units.append(AuditUnit([unit.block], [translated], seg))
                offset += count
    return units


def run_audit(document, image_exists: Callable[[str], bool] | None = None) -> AuditReport:
    source_blocks = document.plan.ir.ast["blocks"]
    return audit_document(
        build_units(document),
        source_blocks,
        document.translated_ast["blocks"],
        image_exists=image_exists,
        source_dead_links=document_ir.collect_link_report(source_blocks)["dead_links"],
        target_dead_links=document.link_report["dead_links"],
    )


def attach_to_segments(document, report: AuditReport) -> int:
    """把带段号的审计问题并入段问题列表（对照阅读器据此标记并导航）；返回并入条数。"""
    owners = {seg.seg_id: item for item in document.chunks for seg in item.structured.segments}
    attached = 0
    for finding in report.findings:
        prefix = _ORIGIN_PREFIX.get(finding.origin)
        item = owners.get(finding.seg_id)
        if prefix is None or item is None or finding.severity not in {"high", "medium"}:
            continue
        item.issues.setdefault(finding.seg_id, []).append(f"{prefix}：{finding.message}")
        attached += 1
    return attached


def image_checker(asset_roots: list[str | Path]) -> Callable[[str], bool]:
    """按导出时解析图片的同一规则判断图片文件是否存在。"""
    from ..postprocess.export_service import ExportService

    roots = [Path(root) for root in asset_roots if Path(root).exists()]

    @functools.cache
    def exists(src: str) -> bool:
        return ExportService._resolve_image_source(Path(src), None, roots) is not None

    return exists
