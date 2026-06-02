"""`todo` 工具。

为模型提供任务清单管理能力，帮助跟踪多步骤 coding 任务的进度。
每个 `create_todo_tool()` 调用创建独立的内存 store，适合单个 agent 会话内使用。

当前是骨架阶段实现：状态保存在内存中，会话结束后丢失。
后续可以接入重新设计后的会话持久化层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.introspection import tool_from_function


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
    ) -> ToolResult:
        """管理会话内任务清单；支持 add/update/delete/list/clear。"""

        if action == "add":
            if not content:
                return make_error_result("todo", "content 不能为空")
            item = store.add(content)
            return _format_result("已添加任务", [item])

        if action == "update":
            if not todo_id:
                return make_error_result("todo", "update 操作需要提供 todo_id")
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

    return tool_from_function(todo)


def _format_result(message: str, items: list[TodoItem]) -> ToolResult:
    """把任务列表格式化为文本结果。"""

    lines: list[str] = [message]
    data: list[dict[str, Any]] = []
    for item in items:
        lines.append(f"{_status_emoji(item.status)} {item.id}: {item.content}")
        data.append({"id": item.id, "content": item.content, "status": item.status})

    content = "\n".join(lines) if items else message
    return make_text_result("todo", content, todos=data, count=len(items))
