"""
模型接口与动作协议。

模型每一步回复一个 JSON 对象，三选一：

    {"thought": "...", "note": "上一条观察的一行结论", "action": "tool", "tool": "名称", "args": {...}}
    {"thought": "...", "note": "...", "action": "submit", "conclusion": {...}, "evidence": ["E1", "E3"]}
    {"thought": "...", "action": "handoff", "reason": "为什么需要人工"}

用文本 JSON 而不用各家的原生 tool calling：DeepSeek、Gemini、本地模型和测试里的假模型
走同一条路径，轨迹可以原样落盘和回放。`note` 是模型对上一条观察的一行总结，
上下文压缩时用它替换整段观察。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..llm.retry import is_retryable, wait_before_retry

Message = dict[str, str]  # {"role": "system" | "user" | "assistant", "content": "..."}


@dataclass
class ModelReply:
    text: str
    tokens: int | None = None  # 服务端报告的 token 数；None 时由运行框架按字符估算


class ChatModel(Protocol):
    async def __call__(self, messages: Sequence[Message], *, metadata: dict | None = None) -> ModelReply: ...


class ProtocolError(ValueError):
    """模型回复不符合动作协议。"""


@dataclass
class Action:
    kind: str  # tool | submit | handoff
    thought: str = ""
    note: str = ""
    tool: str = ""
    args: dict = field(default_factory=dict)
    conclusion: Any = None
    evidence: list[str] = field(default_factory=list)
    reason: str = ""


def parse_action(text: str) -> Action:
    payload = text.strip()
    if not payload.startswith("{"):
        match = re.search(r"\{[\s\S]*\}", payload)
        if not match:
            raise ProtocolError("回复里没有 JSON 对象")
        payload = match.group(0)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"JSON 解析失败：{exc.msg}") from exc
    if not isinstance(data, dict):
        raise ProtocolError("回复必须是 JSON 对象")
    kind = data.get("action")
    common = {"thought": str(data.get("thought", "")), "note": str(data.get("note", ""))}
    if kind == "tool":
        if not isinstance(data.get("tool"), str) or not data["tool"]:
            raise ProtocolError("action=tool 时必须给出 tool 名称")
        args = data.get("args", {})
        if not isinstance(args, dict):
            raise ProtocolError("args 必须是 JSON 对象")
        return Action("tool", tool=data["tool"], args=args, **common)
    if kind == "submit":
        evidence = data.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            raise ProtocolError('evidence 必须是证据编号字符串列表，如 ["E1"]')
        return Action("submit", conclusion=data.get("conclusion"), evidence=evidence, **common)
    if kind == "handoff":
        return Action("handoff", reason=str(data.get("reason", "")), **common)
    raise ProtocolError(f"action 只能是 tool / submit / handoff，实际是 {kind!r}")


def estimate_tokens(text: str) -> int:
    """没有服务端用量时的粗略估算：中文约 1 字 1 token，英文约 4 字符 1 token。"""
    cjk = len(re.findall(r"[㐀-鿿]", text))
    return cjk + (len(text) - cjk + 3) // 4


class ScriptedModel:
    """按顺序返回预设回复的模型：用于测试和按追踪回放。

    回复可以是字符串、ModelReply（回放时带上原始 token 数），或接收当前消息列表、返回字符串的函数。
    """

    def __init__(
        self, replies: Sequence[str | ModelReply | Callable[[Sequence[Message]], str]], tokens: int | None = None
    ):
        self.replies = list(replies)
        self.tokens = tokens
        self.calls: list[list[Message]] = []

    async def __call__(self, messages: Sequence[Message], *, metadata: dict | None = None) -> ModelReply:
        self.calls.append([dict(message) for message in messages])
        if not self.replies:
            raise RuntimeError("ScriptedModel 的预设回复已用完")
        reply = self.replies.pop(0)
        if isinstance(reply, ModelReply):
            return reply
        text = reply(messages) if callable(reply) else reply
        return ModelReply(text, self.tokens)


class RunnableChatModel:
    """把 LangChain 聊天模型（或任意 Runnable）接成 ChatModel。

    callbacks 直接透传给 LangChain：传入 build_langfuse_callbacks(config) 与 UsageMetadataCollector，
    agent 的每一步就和翻译调用一样出现在 Langfuse 追踪与 token 统计里。
    """

    def __init__(self, runnable, callbacks: list | None = None, run_name: str = "agent-step", max_retries: int = 3):
        self.runnable = runnable
        self.callbacks = list(callbacks or [])
        self.run_name = run_name
        self.max_retries = max_retries

    async def __call__(self, messages: Sequence[Message], *, metadata: dict | None = None) -> ModelReply:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        kinds = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}
        converted = [kinds[message["role"]](content=message["content"]) for message in messages]
        config = {"callbacks": self.callbacks, "metadata": dict(metadata or {}), "run_name": self.run_name}
        result = await self._invoke(converted, config)
        if isinstance(result, str):
            return ModelReply(result)
        content = getattr(result, "content", result)
        if isinstance(content, list):
            content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
        usage = getattr(result, "usage_metadata", None) or {}
        return ModelReply(str(content), int(usage["total_tokens"]) if usage.get("total_tokens") else None)

    async def _invoke(self, messages, config):
        """限流、5xx 等可恢复错误按翻译调用同一套退避重试；用尽后抛出，由运行框架把该条目转人工。"""
        for attempt in range(self.max_retries + 1):
            try:
                return await self.runnable.ainvoke(messages, config=config)
            except Exception as exc:
                if not is_retryable(exc) or attempt >= self.max_retries:
                    raise
                await wait_before_retry(exc, attempt)
        raise RuntimeError("unreachable")
