"""
共享工具：内部 agent 与 MCP 用同一份实现。

一个工具就是一个带 `@shared_tool` 标记的 async 函数，第一个参数是 ToolContext，其余参数带类型注解：

    @shared_tool("term_renderings")
    async def term_renderings(ctx: ToolContext, task_id: str, term: str) -> dict:
        \"""查一个词在全书的各种译法。\"""
        return await ctx.service.term_renderings(task_id, term)

ToolSuite 把函数绑定到上下文后得到**同一个可调用对象**，分别注册到：
- 内部 agent 的 ToolRegistry（#15）：参数 schema 由类型注解推出，调用前按 schema 校验；
- MCP（FastMCP.add_tool）：FastMCP 从同一签名生成输入 schema。

写入类工具（writes=True）必须有 `confirm: bool = False` 参数：不带 confirm 时只返回预览，不改变任何数据。
"""

from __future__ import annotations

import functools
import inspect
import types
import typing
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ...infrastructure.agent.tools import ToolRegistry

_MARK = "__shared_tool__"


@dataclass
class ToolContext:
    """工具运行时依赖：任务用例服务（TranslationService）、术语表服务，以及模块自带的其他依赖。"""

    service: Any = None
    glossary_service: Any = None
    extras: dict[str, Any] = field(default_factory=dict)
    # 每次调用前执行一次（如 MCP 进程里惰性初始化任务库）
    prepare: Callable[[], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        if self.glossary_service is None and self.service is not None:
            self.glossary_service = getattr(self.service, "glossary_service", None)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    writes: bool
    function: Callable


def shared_tool(name: str | None = None, *, writes: bool = False, description: str | None = None):
    """标记一个共享工具。description 缺省取函数 docstring 的第一段。"""

    def decorator(function: Callable) -> Callable:
        if not inspect.iscoroutinefunction(function):
            raise TypeError(f"共享工具 {function.__name__} 必须是 async 函数")
        params = list(inspect.signature(function).parameters)
        if not params or params[0] != "ctx":
            raise TypeError(f"共享工具 {function.__name__} 的第一个参数必须是 ctx")
        if writes and "confirm" not in params:
            raise TypeError(f"写入类工具 {function.__name__} 必须有 confirm 参数（默认 False，只预览）")
        doc = inspect.getdoc(function) or ""
        spec = ToolSpec(name or function.__name__, description or doc.split("\n\n", 1)[0].strip(), writes, function)
        setattr(function, _MARK, spec)
        return function

    return decorator


def collect_specs(module) -> list[ToolSpec]:
    """收集一个模块里所有 @shared_tool 函数（按定义顺序）。"""
    return [getattr(value, _MARK) for value in vars(module).values() if hasattr(value, _MARK)]


# --------------------------------------------------------------------------- #
# 类型注解 → ToolRegistry 支持的 JSON Schema 子集
# --------------------------------------------------------------------------- #

_SCALARS = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _schema_for(annotation) -> dict:
    origin = typing.get_origin(annotation)
    args = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
    if origin in (typing.Union, types.UnionType):
        return _schema_for(args[0]) if len(args) == 1 else {}
    if origin is typing.Literal:
        return {"enum": list(typing.get_args(annotation))}
    if annotation in _SCALARS:
        return {"type": _SCALARS[annotation]}
    if origin in (list, tuple) or annotation in (list, tuple):
        return {"type": "array", **({"items": _schema_for(args[0])} if args else {})}
    if origin is dict or annotation is dict:
        return {"type": "object"}
    return {}


def parameters_schema(function: Callable) -> dict:
    """除 ctx 外的参数：有默认值的为可选；None 默认值的参数允许不传（不允许显式传 null）。"""
    hints = typing.get_type_hints(function)
    properties, required = {}, []
    for name, param in list(inspect.signature(function).parameters.items())[1:]:
        properties[name] = _schema_for(hints.get(name, Any))
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def bind(spec: ToolSpec, ctx: ToolContext) -> Callable:
    """把工具函数绑定到上下文。返回的函数签名去掉了 ctx，并带上已解析的类型注解（供 FastMCP 生成 schema）。"""
    function = spec.function

    @functools.wraps(function)
    async def bound(**kwargs):
        if ctx.prepare is not None:
            await ctx.prepare()
        return await function(ctx, **kwargs)

    hints = typing.get_type_hints(function)
    params = [
        param.replace(annotation=hints.get(name, param.annotation))
        for name, param in list(inspect.signature(function).parameters.items())[1:]
    ]
    bound.__signature__ = inspect.Signature(params, return_annotation=hints.get("return", inspect.Signature.empty))
    bound.__annotations__ = {p.name: p.annotation for p in params}
    del bound.__wrapped__  # 让 inspect.signature 用 __signature__，而不是追回带 ctx 的原函数
    return bound


# --------------------------------------------------------------------------- #
# 工具集：一个注册表，两个出口
# --------------------------------------------------------------------------- #


@dataclass
class ToolSuite:
    ctx: ToolContext
    registry: ToolRegistry = field(default_factory=ToolRegistry)
    writes: dict[str, bool] = field(default_factory=dict)

    def add_spec(self, spec: ToolSpec) -> Callable:
        bound = bind(spec, self.ctx)
        self.registry.register(spec.name, spec.description, parameters_schema(spec.function), bound)
        self.writes[spec.name] = spec.writes
        return bound

    def add_module(self, module, names: Iterable[str] | None = None) -> list[str]:
        """注册一个工具模块里的全部（或指定名字的）共享工具，返回注册了哪些。"""
        wanted = set(names) if names is not None else None
        added = []
        for spec in collect_specs(module):
            if wanted is None or spec.name in wanted:
                self.add_spec(spec)
                added.append(spec.name)
        return added

    def add_function(self, function: Callable, name: str | None = None, *, writes: bool = False) -> None:
        """注册一个已绑定好、签名里没有 ctx 的函数（如 MCP server 自带的任务与术语表方法）。"""
        hints = typing.get_type_hints(function)
        properties, required = {}, []
        for param_name, param in inspect.signature(function).parameters.items():
            properties[param_name] = _schema_for(hints.get(param_name, Any))
            if param.default is inspect.Parameter.empty:
                required.append(param_name)
        schema = {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
        doc = (inspect.getdoc(function) or "").split("\n\n", 1)[0].strip()
        tool_name = name or function.__name__
        self.registry.register(tool_name, doc, schema, function)
        self.writes[tool_name] = writes

    def subset(self, names: Iterable[str]) -> ToolRegistry:
        """给内部 agent 用的子集：同一批已绑定函数，只挑需要的工具。"""
        registry = ToolRegistry()
        for name in names:
            tool = self.registry.tools[name]
            registry.register(tool.name, tool.description, tool.parameters, tool.function)
        return registry

    def expose_to_mcp(self, mcp) -> None:
        """把注册表里的每个工具挂到 FastMCP：注册的是同一个函数对象。"""
        from mcp.types import ToolAnnotations

        for tool in self.registry.tools.values():
            writes = self.writes.get(tool.name, False)
            mcp.add_tool(
                tool.function,
                name=tool.name,
                description=inspect.getdoc(tool.function) or tool.description,
                annotations=ToolAnnotations(readOnlyHint=not writes),
            )
