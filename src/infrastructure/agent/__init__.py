"""手写的 agent 运行框架：推理循环、工具注册、预算、上下文压缩、逐条目恢复、追踪与回放。见 ADR-0005。"""

from .context import ContextWindow, EvidenceStore
from .model import Action, ChatModel, ModelReply, ProtocolError, RunnableChatModel, ScriptedModel, parse_action
from .runner import (
    AgentBudget,
    AgentItem,
    AgentOutcome,
    AgentRunner,
    as_graph_node,
    replay,
    summarize_decided,
)
from .tools import Tool, ToolRegistry, ToolResult, validate_args
from .trace import Checkpoint, JsonlTracer, MemoryTracer, read_jsonl

__all__ = [
    "Action",
    "AgentBudget",
    "AgentItem",
    "AgentOutcome",
    "AgentRunner",
    "ChatModel",
    "Checkpoint",
    "ContextWindow",
    "EvidenceStore",
    "JsonlTracer",
    "MemoryTracer",
    "ModelReply",
    "ProtocolError",
    "RunnableChatModel",
    "ScriptedModel",
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "as_graph_node",
    "parse_action",
    "read_jsonl",
    "replay",
    "summarize_decided",
    "validate_args",
]
