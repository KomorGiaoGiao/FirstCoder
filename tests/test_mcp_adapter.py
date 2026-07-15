from __future__ import annotations

from dataclasses import dataclass

import pytest

from firstcoder.mcp.adapter import adapt_mcp_tool
from firstcoder.mcp.models import McpToolDescription
from firstcoder.permissions.types import PermissionAction
from firstcoder.tools.permission_registry import permission_request_for_tool


@dataclass
class FakeManager:
    result: object = None
    error: Exception | None = None

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def call_tool(self, server: str, tool: str, arguments: dict[str, object]) -> object:
        self.calls.append((server, tool, arguments))
        if self.error is not None:
            raise self.error
        return self.result


def test_adapt_mcp_tool_preserves_description_and_object_schema() -> None:
    manager = FakeManager()
    discovered = McpToolDescription(
        "calendar_list",
        "列出指定时间段的日程。",
        {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "开始时间"},
                "limit": {"type": "integer", "description": "最多返回数量"},
            },
            "required": ["start"],
        },
    )

    tool = adapt_mcp_tool(manager, "lark", discovered)

    assert tool.name == "mcp__lark__calendar_list"
    assert tool.definition.description == "列出指定时间段的日程。"
    assert tool.definition.parameters == {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "开始时间"},
            "limit": {"type": "integer", "description": "最多返回数量"},
        },
        "required": ["start"],
    }


def test_adapt_mcp_tool_keeps_optional_properties_out_of_required() -> None:
    tool = adapt_mcp_tool(
        FakeManager(),
        "docs",
        McpToolDescription(
            "read",
            None,
            {
                "type": "object",
                "properties": {"id": {"type": "string"}, "revision": {"type": "integer"}},
                "required": ["id"],
            },
        ),
    )

    assert tool.definition.parameters["required"] == ["id"]
    assert "revision" in tool.definition.parameters["properties"]


@pytest.mark.parametrize("server,tool_name", [("bad.name", "echo"), ("good", "bad name")])
def test_adapt_mcp_tool_rejects_unsafe_server_or_tool_names(server: str, tool_name: str) -> None:
    with pytest.raises(ValueError, match="MCP server/tool 名称不合法"):
        adapt_mcp_tool(FakeManager(), server, McpToolDescription(tool_name, None))


def test_adapt_mcp_tool_rejects_existing_name_collision() -> None:
    with pytest.raises(ValueError, match="MCP 工具名称冲突"):
        adapt_mcp_tool(
            FakeManager(),
            "lark",
            McpToolDescription("calendar_list", None),
            existing_names={"mcp__lark__calendar_list"},
        )


def test_adapt_mcp_tool_returns_error_for_invalid_schema_without_calling_manager() -> None:
    manager = FakeManager()
    tool = adapt_mcp_tool(manager, "lark", McpToolDescription("bad_schema", None, {"type": "array"}))

    result = tool.executor()

    assert result.ok is False
    assert result.error == "MCP 工具参数 schema 无效。"
    assert manager.calls == []


def test_adapt_mcp_tool_renders_text_and_retains_structured_metadata() -> None:
    manager = FakeManager(
        result={
            "content": [{"type": "text", "text": "已找到 2 条日程"}],
            "structuredContent": {"events": [{"id": "evt_1"}, {"id": "evt_2"}]},
        }
    )
    tool = adapt_mcp_tool(manager, "lark", McpToolDescription("calendar_list", None))

    result = tool.executor(limit=2)

    assert result.ok is True
    assert result.content == "已找到 2 条日程"
    assert result.data["mcp"] == {
        "server": "lark",
        "tool": "calendar_list",
        "structured_content": {"events": [{"id": "evt_1"}, {"id": "evt_2"}]},
    }
    assert manager.calls == [("lark", "calendar_list", {"limit": 2})]


def test_adapt_mcp_tool_converts_call_failure_to_safe_error() -> None:
    tool = adapt_mcp_tool(
        FakeManager(error=RuntimeError("Bearer secret-value")), "lark", McpToolDescription("calendar_list", None)
    )

    result = tool.executor()

    assert result.ok is False
    assert result.error == "MCP 工具调用失败。"
    assert "secret-value" not in result.content


def test_adapt_mcp_tool_declares_precise_mcp_permission() -> None:
    tool = adapt_mcp_tool(FakeManager(), "lark", McpToolDescription("calendar_list", None))

    assert tool.permission is not None
    assert tool.permission.action == PermissionAction.MCP_TOOL
    assert tool.permission.target_value == "lark/calendar_list"
    assert tool.permission.allow_auto is False

    request = permission_request_for_tool(tool, {"limit": 2})

    assert request.action == PermissionAction.MCP_TOOL
    assert request.target == "lark/calendar_list"
