"""
追踪与落盘。

- Tracer：记录每一步的输入消息、模型回复、token、工具调用与观察，以及每个条目的结局。
  JsonlTracer 逐行追加写文件，崩溃时已写的步骤不丢。模型调用本身还会经 LangChain
  回调进入 Langfuse（见 model.RunnableChatModel），两者互补：Langfuse 看调用链，
  JSONL 用来回放。
- Checkpoint：每完成一个条目追加一行结局；重新运行时跳过已完成的条目。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol


class Tracer(Protocol):
    def record(self, event: dict) -> None: ...


class MemoryTracer:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, event: dict) -> None:
        self.events.append(json.loads(json.dumps(event, ensure_ascii=False, default=str)))


class JsonlTracer:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: dict) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def events(self) -> list[dict]:
        return read_jsonl(self.path)


def read_jsonl(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:  # 崩溃时写了一半的最后一行
            continue
    return events


class Checkpoint:
    """逐条目落盘的结局记录。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, dict]:
        return {record["item_id"]: record for record in read_jsonl(self.path)}

    def save(self, outcome: dict) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(outcome, ensure_ascii=False, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
