"""
术语账本的持久化：把每次翻译自动生成的统一译名保存为普通术语表，供后台随时审阅。

- 文件：`data/glossaries/<书名>_术语账本.json`，在 Web "术语表" 页面可见、可编辑；
- 下次翻译同一本书时自动读取：优先级 用户所选术语表 > 已保存的账本（含人工修改）> 本次新识别；
- 合并只追加新词、不覆盖已有条目，人工改过的译名不会被下一次运行冲掉。
审阅是可选的：不审也能保证同一本书多次运行的译名一致。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from ...infrastructure.persistence.glossary_repository import GlossaryService

LEDGER_SUFFIX = "术语账本"


def ledger_glossary_id(filename: str) -> str:
    """书名 → 账本术语表 id（与 GlossaryService.create_glossary 的命名规则一致）。"""
    stem = Path(filename).stem
    slug = re.sub(r"[^\w-]+", "_", f"{stem} {LEDGER_SUFFIX}".strip().lower(), flags=re.UNICODE)
    slug = re.sub(r"[_-]{2,}", "_", slug).strip("_-")
    return slug or LEDGER_SUFFIX


async def load_ledger(service: GlossaryService, filename: str) -> dict[str, str]:
    saved = await service.get_glossary(ledger_glossary_id(filename))
    return dict(saved["terms"]) if saved else {}


def merged_glossary(user_glossary: dict[str, str], saved_ledger: dict[str, str]) -> dict[str, str]:
    """用户术语表优先，已保存账本补充缺失条目（大小写不敏感地判断重复）。"""
    merged = dict(user_glossary)
    existing = {key.lower() for key in merged}
    for term, translation in saved_ledger.items():
        if term.lower() not in existing:
            merged[term] = translation
            existing.add(term.lower())
    return merged


async def save_ledger(service: GlossaryService, filename: str, new_terms: dict[str, str]) -> tuple[str, int]:
    """把本次新识别的条目追加进账本术语表，返回 (术语表 id, 新增条数)。已有条目（含人工修改）保持不变。"""
    glossary_id = ledger_glossary_id(filename)
    if not new_terms:
        return glossary_id, 0
    path = service._path(glossary_id)

    def write() -> int:
        with service._lock(glossary_id):
            current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            known = {key.lower() for key in current}
            added = {term: zh for term, zh in new_terms.items() if term.lower() not in known}
            if added or not path.exists():
                service._write(path, {**current, **added})
            return len(added)

    return glossary_id, await asyncio.to_thread(write)


__all__ = ["ledger_glossary_id", "load_ledger", "merged_glossary", "save_ledger"]
