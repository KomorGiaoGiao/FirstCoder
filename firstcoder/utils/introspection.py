"""根据 Python 函数签名生成工具定义。"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, get_origin, get_type_hints

from firstcoder.utils.schema import object_schema, property_schema
from firstcoder.providers.types import ToolDefinition

if TYPE_CHECKING:
    from firstcoder.tools.types import Tool, ToolResult


PYTHON_TYPE_TO_JSON_TYPE: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
    type(None): "null",
}


def function_to_parameters(func: Callable[..., "ToolResult"]) -> dict[str, Any]:
    """根据函数签名生成工具参数 JSON Schema。"""

    signature = inspect.signature(func)
    type_hints = get_type_hints(func)
    properties: dict[str, dict[str, Any]] = {}
    required: list[str] = []

    for parameter in signature.parameters.values():
        if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise ValueError(f"不支持可变参数：{parameter.name}")

        if parameter.name == "self":
            continue

        annotation = type_hints.get(parameter.name, parameter.annotation)
        properties[parameter.name] = property_schema(_annotation_to_json_type(annotation))
        if parameter.default is inspect.Signature.empty:
            required.append(parameter.name)

    return object_schema(properties, required=required)


def tool_from_function(
    func: Callable[..., "ToolResult"],
    *,
    name: str | None = None,
    description: str | None = None,
) -> "Tool":
    """根据函数自动创建模型可调用工具。"""

    tool_name = name or func.__name__
    tool_description = description if description is not None else inspect.getdoc(func) or ""

    from firstcoder.tools.types import Tool

    return Tool(
        definition=ToolDefinition(
            name=tool_name,
            description=tool_description,
            parameters=function_to_parameters(func),
        ),
        executor=func,
    )


def _annotation_to_json_type(annotation: Any) -> str:
    """把 Python 类型注解转换成 JSON Schema 类型。"""

    if annotation is inspect.Signature.empty:
        return "string"

    if annotation in PYTHON_TYPE_TO_JSON_TYPE:
        return PYTHON_TYPE_TO_JSON_TYPE[annotation]

    origin = get_origin(annotation)
    if origin in PYTHON_TYPE_TO_JSON_TYPE:
        return PYTHON_TYPE_TO_JSON_TYPE[origin]

    return "string"
