"""内部工具定义到各家 provider 工具格式的转换函数。"""

from __future__ import annotations

from typing import Any

from firstcoder.providers.types import ToolDefinition


def to_openai_tool(tool: ToolDefinition) -> dict[str, Any]:
    """把项目内部工具定义转换为 OpenAI function tool 格式。"""

    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def to_anthropic_tool(tool: ToolDefinition) -> dict[str, Any]:
    """把项目内部工具定义转换为 Anthropic tool use 格式。"""

    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.parameters,
    }
