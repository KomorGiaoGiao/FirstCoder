"""`todo` 工具测试。"""

from __future__ import annotations

from firstcoder.tools.todo import create_todo_tool


def test_todo_adds_item():
    tool = create_todo_tool()

    result = tool.executor(action="add", content="修复登录 bug")

    assert result.ok is True
    assert result.name == "todo"
    assert "修复登录 bug" in result.content
    assert result.data["todos"][0]["content"] == "修复登录 bug"
    assert result.data["todos"][0]["status"] == "pending"


def test_todo_lists_items():
    tool = create_todo_tool()
    tool.executor(action="add", content="任务 A")
    tool.executor(action="add", content="任务 B")

    result = tool.executor(action="list")

    assert result.ok is True
    assert "任务 A" in result.content
    assert "任务 B" in result.content
    assert len(result.data["todos"]) == 2


def test_todo_updates_status():
    tool = create_todo_tool()
    add_result = tool.executor(action="add", content="任务 A")
    todo_id = add_result.data["todos"][0]["id"]

    result = tool.executor(action="update", todo_id=todo_id, status="done")

    assert result.ok is True
    assert result.data["todos"][0]["status"] == "done"


def test_todo_updates_content():
    tool = create_todo_tool()
    add_result = tool.executor(action="add", content="旧内容")
    todo_id = add_result.data["todos"][0]["id"]

    result = tool.executor(action="update", todo_id=todo_id, content="新内容")

    assert result.ok is True
    assert result.data["todos"][0]["content"] == "新内容"


def test_todo_deletes_item():
    tool = create_todo_tool()
    add_result = tool.executor(action="add", content="待删除")
    todo_id = add_result.data["todos"][0]["id"]

    result = tool.executor(action="delete", todo_id=todo_id)

    assert result.ok is True
    assert len(result.data["todos"]) == 0
    assert "已删除" in result.content


def test_todo_delete_unknown_id_returns_error():
    tool = create_todo_tool()

    result = tool.executor(action="delete", todo_id="unknown")

    assert result.ok is False
    assert "不存在" in result.error


def test_todo_update_unknown_id_returns_error():
    tool = create_todo_tool()

    result = tool.executor(action="update", todo_id="unknown", status="done")

    assert result.ok is False
    assert "不存在" in result.error


def test_todo_clear_removes_all():
    tool = create_todo_tool()
    tool.executor(action="add", content="任务 A")
    tool.executor(action="add", content="任务 B")

    result = tool.executor(action="clear")

    assert result.ok is True
    assert len(result.data["todos"]) == 0


def test_todo_add_requires_content():
    tool = create_todo_tool()

    result = tool.executor(action="add")

    assert result.ok is False
    assert "content 不能为空" in result.error


def test_todo_shows_status_emoji():
    tool = create_todo_tool()
    add_result = tool.executor(action="add", content="任务")
    todo_id = add_result.data["todos"][0]["id"]
    tool.executor(action="update", todo_id=todo_id, status="done")

    result = tool.executor(action="list")

    assert result.ok is True
    assert "[x]" in result.content or "done" in result.content


def test_todo_definition_has_correct_schema():
    tool = create_todo_tool()

    assert tool.name == "todo"
    assert "action" in tool.definition.parameters["properties"]
    assert "content" in tool.definition.parameters["properties"]
    assert "todo_id" in tool.definition.parameters["properties"]
    assert "status" in tool.definition.parameters["properties"]
    assert tool.definition.parameters["required"] == ["action"]
