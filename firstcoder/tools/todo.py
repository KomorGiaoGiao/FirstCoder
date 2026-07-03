"""`todo` 工具。

为模型提供任务清单管理能力，帮助跟踪多步骤 coding 任务的进度。
每个 `create_todo_tool()` 调用创建独立的内存 store，适合单个 agent 会话内使用。

当前是骨架阶段实现：状态保存在内存中，会话结束后丢失。
后续可以接入重新设计后的会话持久化层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from firstcoder.providers.types import ToolDefinition
from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.schema import object_schema, property_schema


@dataclass
class TodoItem:
    """单个任务项。"""

    id: str
    content: str
    status: str = "pending"


class TodoStore:
    """内存中的任务清单存储。"""

    def __init__(self) -> None:
        self._todos: dict[str, TodoItem] = {}
        self._counter = 0

    def add(self, content: str) -> TodoItem:
        """添加新任务。"""

        self._counter += 1
        item = TodoItem(id=f"todo_{self._counter}", content=content)
        self._todos[item.id] = item
        return item

    def replace_all(self, todos: list[dict[str, Any]]) -> list[TodoItem]:
        """用一组任务替换当前清单。"""

        self._todos.clear()
        self._counter = 0
        items: list[TodoItem] = []
        for todo in todos:
            content = str(todo.get("content") or "").strip()
            status = str(todo.get("status") or "pending")
            self._counter += 1
            item = TodoItem(id=f"todo_{self._counter}", content=content, status=status)
            self._todos[item.id] = item
            items.append(item)
        return items

    def update(self, todo_id: str, content: str | None = None, status: str | None = None) -> TodoItem | None:
        """更新任务内容或状态。"""

        item = self._todos.get(todo_id)
        if item is None:
            return None
        if content is not None:
            item.content = content
        if status is not None:
            item.status = status
        return item

    def delete(self, todo_id: str) -> bool:
        """删除任务。"""

        if todo_id not in self._todos:
            return False
        del self._todos[todo_id]
        return True

    def list_all(self) -> list[TodoItem]:
        """返回所有任务，按添加顺序。"""

        return list(self._todos.values())

    def clear(self) -> None:
        """清空所有任务。"""

        self._todos.clear()


VALID_STATUSES = {"pending", "in_progress", "done"}


def _status_emoji(status: str) -> str:
    """状态对应的展示符号。"""

    if status == "done":
        return "[x]"
    if status == "in_progress":
        return "[~]"
    return "[ ]"


def create_todo_tool() -> Tool:
    """创建任务清单管理工具。"""

    store = TodoStore()

    def todo(
        action: str,
        content: str | None = None,
        todo_id: str | None = None,
        status: str | None = None,
        todos: list[dict[str, Any]] | None = None,
    ) -> ToolResult:
        """管理会话内任务清单；支持 set/add/update/delete/list/clear。"""

        if action == "set":
            if not todos:
                return make_error_result("todo", "set 操作需要提供 todos")
            invalid = _first_invalid_todo(todos)
            if invalid:
                return make_error_result("todo", invalid)
            items = store.replace_all(todos)
            return _format_result("已设置任务清单", items)

        if action == "add":
            if not content:
                return make_error_result("todo", "content 不能为空")
            if status is not None and status not in VALID_STATUSES:
                return make_error_result("todo", f"未知状态：{status}")
            item = store.add(content)
            if status is not None:
                item.status = status
            return _format_result("已添加任务", [item])

        if action == "update":
            if not todo_id:
                return make_error_result("todo", "update 操作需要提供 todo_id")
            if status is not None and status not in VALID_STATUSES:
                return make_error_result("todo", f"未知状态：{status}")
            item = store.update(todo_id, content=content, status=status)
            if item is None:
                return make_error_result("todo", f"任务不存在：{todo_id}")
            return _format_result("已更新任务", list(store.list_all()))

        if action == "delete":
            if not todo_id:
                return make_error_result("todo", "delete 操作需要提供 todo_id")
            if not store.delete(todo_id):
                return make_error_result("todo", f"任务不存在：{todo_id}")
            return _format_result("已删除任务", list(store.list_all()))

        if action == "list":
            items = store.list_all()
            return _format_result("任务清单" if items else "暂无任务", items)

        if action == "clear":
            store.clear()
            return _format_result("已清空任务清单", [])

        return make_error_result("todo", f"未知操作：{action}")

    return Tool(
        definition=ToolDefinition(
            name="todo",
            description=(
                "Track progress for multi-step work. Prefer action='set' once at the start "
                "to create the whole plan, then action='update' as items move through "
                "pending, in_progress, and done. Keep exactly one item in_progress."
            ),
            parameters=object_schema(
                {
                    "action": property_schema(
                        "string",
                        enum=["set", "add", "update", "delete", "list", "clear"],
                        description=(
                            "set replaces the whole plan; add creates one item; update changes "
                            "content or status; delete removes one item; list shows the plan; "
                            "clear removes all items."
                        ),
                    ),
                    "content": property_schema("string", description="Todo text for add or update."),
                    "todo_id": property_schema(
                        "string",
                        description="Existing id such as todo_1. Required for update and delete.",
                    ),
                    "status": property_schema(
                        "string",
                        enum=["pending", "in_progress", "done"],
                        description="Use exactly one in_progress item while work is underway.",
                    ),
                    "todos": {
                        "type": "array",
                        "description": (
                            "Full plan for action='set'. Use this instead of multiple add calls "
                            "when creating an initial checklist."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "done"],
                                },
                            },
                            "required": ["content"],
                        },
                    },
                },
                required=["action"],
            ),
        ),
        executor=todo,
    )


def _first_invalid_todo(todos: list[dict[str, Any]]) -> str | None:
    for index, todo in enumerate(todos, start=1):
        if not str(todo.get("content") or "").strip():
            return f"todos[{index}] 缺少 content"
        status = str(todo.get("status") or "pending")
        if status not in VALID_STATUSES:
            return f"todos[{index}] 未知状态：{status}"
    return None


def _format_result(message: str, items: list[TodoItem]) -> ToolResult:
    """把任务列表格式化为文本结果。"""

    lines: list[str] = [message]
    data: list[dict[str, Any]] = []
    for item in items:
        lines.append(f"{_status_emoji(item.status)} {item.id}: {item.content}")
        data.append({"id": item.id, "content": item.content, "status": item.status})

    content = "\n".join(lines) if items else message
    return make_text_result("todo", content, todos=data, count=len(items))
