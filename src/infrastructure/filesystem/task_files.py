"""任务文件的唯一入口；名称、资源路径与执行产物相互隔离。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path


def safe_id(value: str) -> str:
    if not re.fullmatch(r"[\w-]{1,160}", value, flags=re.UNICODE):
        raise ValueError("无效的文件标识")
    return value


def validate_upload(content: bytes, filename: str, max_bytes: int = 50 * 1024 * 1024) -> str:
    name = Path((filename or "").replace("\\", "/")).name
    if not name or name != filename or "\x00" in name or len(name) > 200:
        raise ValueError("文件名无效，请使用不包含目录的文件名")
    suffix = Path(name).suffix.lower()
    if suffix not in {".md", ".markdown", ".pdf", ".epub"}:
        raise ValueError("仅支持 Markdown、PDF 或 EPUB")
    if not content or len(content) > max_bytes:
        raise ValueError("文件不能为空，且不得超过 50 MB")
    if suffix in {".md", ".markdown"}:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Markdown 文件必须使用 UTF-8 编码") from exc
        if not text.strip() or "\x00" in text:
            raise ValueError("Markdown 文件内容无效")
    if suffix == ".pdf" and not content.startswith(b"%PDF-"):
        raise ValueError("文件不是有效的 PDF")
    if suffix == ".epub" and not content.startswith(b"PK"):
        raise ValueError("文件不是有效的 EPUB")
    return name


class TaskFiles:
    def __init__(self, root: str | Path = "data"):
        self.root = Path(root)

    def upload(self, task):
        safe_id(task.task_id)
        name = Path(task.filename.replace("\\", "/")).name
        if name != task.filename:
            raise ValueError("历史任务文件名包含非法路径")
        return self.root / "uploads" / f"{task.task_id}_{name}"

    def attempt(self, task):
        return self.root / "results" / "runs" / safe_id(task.task_id) / safe_id(task.execution_id)

    def artifacts(self, task):
        if task.artifact_dir:
            path = Path(task.artifact_dir).resolve()
            if not path.is_relative_to((self.root / "results" / "runs" / safe_id(task.task_id)).resolve()):
                raise ValueError("任务产物目录无效")
            return path
        return self.root / "results"

    def result(self, task, bilingual=False):
        suffix = ".bilingual.md" if bilingual else ".md"
        return self.artifacts(task) / f"{safe_id(task.task_id)}{suffix}"

    def records(self, task):
        path = self.artifacts(task) / f"{safe_id(task.task_id)}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    @staticmethod
    def save_records(directory, task_id, results):
        path = Path(directory) / f"{safe_id(task_id)}.json"
        path.write_text(
            json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2), encoding="utf-8"
        )
