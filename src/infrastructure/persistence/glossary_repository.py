"""
术语表管理服务
"""

import asyncio
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from ..filesystem.task_files import safe_id


class GlossaryService:
    """术语表管理服务"""

    def __init__(self):
        self.glossary_dir = Path("data/glossaries")
        self.glossary_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, glossary_id):
        return self.glossary_dir / f"{safe_id(glossary_id)}.json"

    @contextmanager
    def _lock(self, glossary_id):
        with (self.glossary_dir / f".{safe_id(glossary_id)}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @staticmethod
    def _validate_terms(terms):
        if not isinstance(terms, dict) or any(
            not isinstance(k, str) or not k.strip() or not isinstance(v, str) or not v.strip() for k, v in terms.items()
        ):
            raise ValueError("术语表必须是非空字符串到非空字符串的 JSON 对象")
        return terms

    def _write(self, path, terms):
        self._validate_terms(terms)
        fd, name = tempfile.mkstemp(dir=self.glossary_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(terms, f, ensure_ascii=False, indent=2)
            os.replace(name, path)
        finally:
            if Path(name).exists():
                Path(name).unlink()

    async def list_glossaries(self) -> list[dict]:
        """获取术语表列表"""
        result = []

        for file_path in self.glossary_dir.glob("*.json"):
            try:
                with open(file_path, encoding="utf-8") as f:
                    terms = json.load(f)

                result.append(
                    {
                        "id": file_path.stem,
                        "name": file_path.stem.replace("_", " ").title(),
                        "term_count": len(terms),
                        "updated_at": datetime.fromtimestamp(file_path.stat().st_mtime).isoformat(),
                    }
                )
            except Exception:
                continue

        return sorted(result, key=lambda x: x["updated_at"], reverse=True)

    async def get_glossary(self, glossary_id: str) -> dict | None:
        """获取术语表内容"""
        file_path = self._path(glossary_id)

        if not file_path.exists():
            return None

        try:
            with open(file_path, encoding="utf-8") as f:
                terms = json.load(f)

            return {"id": glossary_id, "name": glossary_id.replace("_", " ").title(), "terms": terms}
        except Exception:
            return None

    async def create_glossary(self, name: str, terms: dict[str, str]) -> dict:
        """创建术语表"""
        glossary_id = name.lower().replace(" ", "_")
        file_path = self._path(glossary_id)

        self._validate_terms(terms)
        # O_EXCL 保证同名并发创建也不会覆盖。
        with open(file_path, "x", encoding="utf-8") as f:
            json.dump(terms, f, ensure_ascii=False, indent=2)

        return {"id": glossary_id, "name": name, "term_count": len(terms)}

    async def update_glossary(self, glossary_id: str, terms: dict[str, str]) -> bool:
        """更新术语表 (全量)"""
        file_path = self._path(glossary_id)

        self._validate_terms(terms)

        def update():
            with self._lock(glossary_id):
                if not file_path.exists():
                    return False
                self._write(file_path, terms)
                return True

        return await asyncio.to_thread(update)

    async def modify_terms(self, glossary_id: str, add: dict[str, str] = None, remove: list[str] = None) -> int | None:
        """增量修改术语"""
        path = self._path(glossary_id)

        def modify():
            # 锁覆盖整个读改写，避免多个 API 进程互相丢失增量更新。
            with self._lock(glossary_id):
                if not path.exists():
                    return None
                terms = self._validate_terms(json.loads(path.read_text(encoding="utf-8")))
                terms.update(add or {})
                for key in remove or []:
                    terms.pop(key, None)
                self._write(path, terms)
                return len(terms)

        return await asyncio.to_thread(modify)

    async def delete_glossary(self, glossary_id: str) -> bool:
        """删除术语表"""
        file_path = self._path(glossary_id)

        def delete():
            with self._lock(glossary_id):
                if not file_path.exists():
                    return False
                file_path.unlink()
                return True

        return await asyncio.to_thread(delete)

    async def import_from_file(self, file_content: bytes, name: str) -> dict:
        """从文件导入"""
        terms = json.loads(file_content.decode("utf-8"))
        return await self.create_glossary(name, terms)

    async def export_to_file(self, glossary_id: str) -> Path | None:
        """导出到文件"""
        file_path = self._path(glossary_id)

        if not file_path.exists():
            return None

        return file_path
