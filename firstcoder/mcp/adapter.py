"""把发现到的 MCP 工具转换为 FirstCoder 同步工具。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Protocol

from firstcoder.mcp.models import McpToolDescription
from firstcoder.permissions.types import PermissionAction
from firstcoder.providers.types import ToolDefinition
from firstcoder.tools.types import Tool, ToolPermissionSpec, ToolResult, make_error_result

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class McpToolCaller(Protocol):
    """适配器所需的同步 MCP 调用入口。"""

    def call_tool(self, server: str, tool: str, arguments: dict[str, object]) -> object: ...


def adapt_mcp_tool(
    manager: McpToolCaller,
    server: str,
    discovered_tool: McpToolDescription,
    *,
    existing_names: set[str] | None = None,
) -> Tool:
    """把一个发现到的 MCP tool 转换成经过既有权限链路执行的 ``Tool``。"""

    tool_name = discovered_tool.name
    if not _SAFE_NAME.fullmatch(server) or not _SAFE_NAME.fullmatch(tool_name):
        raise ValueError("MCP server/tool 名称不合法")
    name = f"mcp__{server}__{tool_name}"
    if name in (existing_names or set()):
        raise ValueError(f"MCP 工具名称冲突：{name}")
    try:
        parameters = _tool_parameters(discovered_tool.input_schema)
    except ValueError:
        parameters = {"type": "object", "properties": {}}
        schema_error = True
    else:
        schema_error = False

    def execute(**arguments: Any) -> ToolResult:
        if schema_error:
            return make_error_result(name, "MCP 工具参数 schema 无效。")
        try:
            result = manager.call_tool(server, tool_name, dict(arguments))
        except Exception:
            return make_error_result(name, "MCP 工具调用失败。")
        return _tool_result(name, server, tool_name, result)

    return Tool(
        definition=ToolDefinition(
            name=name,
            description=discovered_tool.description or f"调用 MCP 工具 {server}/{tool_name}。",
            parameters=parameters,
        ),
        executor=execute,
        permission=ToolPermissionSpec(
            action=PermissionAction.MCP_TOOL,
            target_value=f"{server}/{tool_name}",
            allow_auto=False,
            reason=f"调用 MCP 工具 {server}/{tool_name}。",
        ),
    )


def _tool_parameters(input_schema: Mapping[str, object]) -> dict[str, object]:
    """验证 MCP 的输入 schema，并复制为 provider 需要的对象 schema。"""

    schema = dict(input_schema)
    if not schema:
        return {"type": "object", "properties": {}}
    if schema.get("type") != "object":
        raise ValueError("MCP input schema 必须是 object")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, Mapping) or not all(isinstance(key, str) for key in properties):
        raise ValueError("MCP input schema properties 无效")
    if not isinstance(required, list) or not all(isinstance(key, str) and key in properties for key in required):
        raise ValueError("MCP input schema required 无效")
    return schema


def _tool_result(name: str, server: str, tool: str, result: object) -> ToolResult:
    """将 MCP 结果转换为模型可读文本，并保留结构化结果。"""

    content = _field(result, "content", ())
    structured_content = _field(result, "structuredContent", None)
    text = _render_content(content)
    data: dict[str, Any] = {"mcp": {"server": server, "tool": tool}}
    if structured_content is not None:
        data["mcp"]["structured_content"] = structured_content
    if bool(_field(result, "isError", False)):
        message = text or "MCP 工具返回错误。"
        return ToolResult(name=name, ok=False, content=message, data=data, error=message)
    return ToolResult(name=name, ok=True, content=text or "MCP 工具调用完成。", data=data)


def _field(value: object, name: str, default: object) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _render_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return _safe_text(content)
    rendered: list[str] = []
    for item in content:
        if _field(item, "type", "") == "text":
            text = _field(item, "text", "")
            if isinstance(text, str) and text:
                rendered.append(text)
        else:
            rendered.append(_safe_text(item))
    return "\n".join(part for part in rendered if part)


def _safe_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)
