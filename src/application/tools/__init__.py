"""共享工具注册表：内部 agent 与 MCP 用同一套工具、同一段实现。

挂接新工具模块（如术语 agent、审校 agent 的工具）：
1. 新建模块，用 `@shared_tool` 写 async 函数（第一个参数 ctx: ToolContext，其余参数带类型注解；
   写入类工具加 writes=True 并带 `confirm: bool = False`，不确认只预览）；
2. 把模块加进 DEFAULT_MODULES，它就会同时出现在 MCP 和 build_tool_suite 的注册表里；
   只给内部 agent 用、不想暴露给 MCP 的模块，调用 build_tool_suite(ctx, modules=[...]) 单独组装即可。

内部 agent 取工具子集：
    suite = build_tool_suite(ToolContext(service=translation_service))
    registry = suite.subset(["term_occurrences", "term_renderings", "read_segment"])
    AgentRunner(model, registry, ...)  # 见 src/infrastructure/agent
"""

from __future__ import annotations

from collections.abc import Iterable

from . import audit, review, search, terms
from .base import ToolContext, ToolSpec, ToolSuite, collect_specs, parameters_schema, shared_tool

DEFAULT_MODULES = [review, terms, audit, search]


def build_tool_suite(
    ctx: ToolContext, modules: Iterable | None = None, names: Iterable[str] | None = None
) -> ToolSuite:
    """把工具模块绑定到上下文，组装成一个注册表。names 可只挑部分工具。"""
    suite = ToolSuite(ctx)
    wanted = set(names) if names is not None else None
    for module in DEFAULT_MODULES if modules is None else modules:
        suite.add_module(module, wanted)
    return suite


__all__ = [
    "DEFAULT_MODULES",
    "ToolContext",
    "ToolSpec",
    "ToolSuite",
    "build_tool_suite",
    "collect_specs",
    "parameters_schema",
    "shared_tool",
]
