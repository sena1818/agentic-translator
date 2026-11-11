"""
工具注册表：每个工具声明名称、说明和参数 schema。

调用前按 schema 校验参数；校验失败或工具抛错都不会中断运行，而是变成一条观察结果
返回给模型，让它自己改正。schema 只支持 JSON Schema 的常用子集（object / string /
integer / number / boolean / array、required、enum、minimum / maximum、
additionalProperties=false），不引入额外依赖。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

ToolFunction = Callable[..., Any] | Callable[..., Awaitable[Any]]

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
}


def validate_args(schema: dict, value: Any, path: str = "args") -> list[str]:
    """按 schema 校验参数，返回错误描述列表（空列表表示通过）。"""
    errors: list[str] = []
    expected = schema.get("type")
    if expected:
        types = _TYPES.get(expected)
        is_bool = isinstance(value, bool)
        if types is None:
            return [f"{path}: schema 使用了不支持的类型 {expected}"]
        if not isinstance(value, types) or (is_bool and expected in {"integer", "number"}):
            return [f"{path} 应为 {expected}，实际是 {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} 只能取 {schema['enum']}，实际是 {value!r}")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} 不能小于 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} 不能大于 {schema['maximum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path} 缺少必填参数 {name}")
        for name, item in value.items():
            if name in properties:
                errors += validate_args(properties[name], item, f"{path}.{name}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path} 不接受参数 {name}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors += validate_args(schema["items"], item, f"{path}[{index}]")
    return errors


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    function: ToolFunction

    def describe(self) -> str:
        return f"- {self.name}: {self.description}\n  参数 schema: {json.dumps(self.parameters, ensure_ascii=False)}"


@dataclass
class ToolResult:
    """一次工具调用的结果。ok=False 时 output 是给模型看的错误说明。"""

    ok: bool
    output: str


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, name: str, description: str, parameters: dict, function: ToolFunction) -> Tool:
        if name in self.tools:
            raise ValueError(f"工具 {name} 已注册")
        tool = Tool(name, description, parameters, function)
        self.tools[name] = tool
        return tool

    def tool(self, name: str, description: str, parameters: dict):
        """装饰器形式的注册。"""

        def decorator(function: ToolFunction) -> ToolFunction:
            self.register(name, description, parameters, function)
            return function

        return decorator

    def names(self) -> list[str]:
        return list(self.tools)

    def describe(self) -> str:
        return "\n".join(tool.describe() for tool in self.tools.values())

    async def call(self, name: str, args: Any) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(False, f"没有名为 {name} 的工具。可用工具：{', '.join(self.tools) or '无'}")
        errors = validate_args(tool.parameters, args)
        if errors:
            return ToolResult(False, "参数校验失败：" + "；".join(errors))
        try:
            result = tool.function(**args)
            if inspect.isawaitable(result):
                result = await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 工具出错交给模型处理，不中断运行
            return ToolResult(False, f"工具 {name} 执行出错：{type(exc).__name__}: {exc}")
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)
        return ToolResult(True, result)
