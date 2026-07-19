"""无状态的 Session Todo 完整列表工具。"""

from __future__ import annotations

from typing import Any

from firstcoder.providers.types import ToolDefinition
from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.schema import object_schema


VALID_STATUSES = ("pending", "in_progress", "completed", "cancelled")
LEGACY_STATUS_ALIASES = {"done": "completed"}


def create_todo_tool() -> Tool:
    """创建只负责验证和规范化完整列表的 Todo 工具。"""

    def todo(todos: list[dict[str, Any]]) -> ToolResult:
        normalized, error = _normalize_todos(todos)
        if error is not None:
            return make_error_result("todo", error)
        return _format_result(normalized)

    return Tool(
        definition=ToolDefinition(
            name="todo",
            description=(
                "Replace the current Todo list for multi-step work. Each item has content and status; "
                "at most one item may be in_progress. An empty list clears Todo state."
            ),
            parameters=object_schema(
                {
                    "todos": {
                        "type": "array",
                        "description": "The complete current Todo list, in display and execution order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "description": "Short, concrete, verifiable task."},
                                "status": {"type": "string", "enum": list(VALID_STATUSES)},
                            },
                            "required": ["content", "status"],
                            "additionalProperties": False,
                        },
                    }
                },
                required=["todos"],
            ),
        ),
        executor=todo,
    )


def _normalize_todos(todos: object) -> tuple[list[dict[str, str]], str | None]:
    if not isinstance(todos, list):
        return [], "todos 必须是数组"

    normalized: list[dict[str, str]] = []
    for index, item in enumerate(todos, start=1):
        if not isinstance(item, dict):
            return [], f"todos[{index}] 必须是对象"
        content = str(item.get("content") or "").strip()
        if not content:
            return [], f"todos[{index}] 缺少 content"
        status = str(item.get("status") or "pending")
        status = LEGACY_STATUS_ALIASES.get(status, status)
        if status not in VALID_STATUSES:
            return [], f"todos[{index}] 未知状态：{status}"
        normalized.append({"content": content, "status": status})
    if sum(item["status"] == "in_progress" for item in normalized) > 1:
        return [], "Todo 列表最多只能有一个 in_progress 项"
    return normalized, None


def _format_result(todos: list[dict[str, str]]) -> ToolResult:
    return make_text_result("todo", "Todo updated", todos=todos, count=len(todos))
