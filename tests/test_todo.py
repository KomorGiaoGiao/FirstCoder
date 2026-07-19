"""`todo` 工具测试。"""

from __future__ import annotations

from firstcoder.tools.todo import create_todo_tool


def test_todo_replaces_complete_list_without_internal_state() -> None:
    tool = create_todo_tool()

    first = tool.executor(
        todos=[
            {"content": "读代码", "status": "in_progress", "priority": "high"},
            {"content": "跑测试", "status": "pending", "priority": "medium"},
        ]
    )
    second = tool.executor(
        todos=[
            {"content": "总结", "status": "completed", "priority": "low"},
        ]
    )

    assert first.ok is True
    assert first.data["todos"] == [
        {"content": "读代码", "status": "in_progress"},
        {"content": "跑测试", "status": "pending"},
    ]
    assert second.data["todos"] == [
        {"content": "总结", "status": "completed"},
    ]
    assert "id" not in second.data["todos"][0]


def test_todo_accepts_empty_list_to_clear_session_state() -> None:
    result = create_todo_tool().executor(todos=[])

    assert result.ok is True
    assert result.data == {"todos": [], "count": 0}


def test_todo_normalizes_defaults_and_legacy_done_status() -> None:
    result = create_todo_tool().executor(
        todos=[
            {"content": "待处理"},
            {"content": "已处理", "status": "done"},
        ]
    )

    assert result.ok is True
    assert result.data["todos"] == [
        {"content": "待处理", "status": "pending"},
        {"content": "已处理", "status": "completed"},
    ]
    assert result.content == "Todo updated"


def test_todo_accepts_cancelled_status() -> None:
    result = create_todo_tool().executor(
        todos=[{"content": "不再需要", "status": "cancelled", "priority": "low"}]
    )

    assert result.ok is True
    assert result.data["todos"][0]["status"] == "cancelled"
    assert result.content == "Todo updated"


def test_todo_rejects_multiple_in_progress_items() -> None:
    result = create_todo_tool().executor(
        todos=[
            {"content": "检查实现", "status": "in_progress"},
            {"content": "运行测试", "status": "in_progress"},
        ]
    )

    assert result.ok is False
    assert "in_progress" in result.error


def test_todo_returns_compact_text_and_structured_state() -> None:
    result = create_todo_tool().executor(
        todos=[{"content": "检查实现", "status": "in_progress"}]
    )

    assert result.ok is True
    assert result.content == "Todo updated"
    assert result.data["todos"] == [{"content": "检查实现", "status": "in_progress"}]


def test_todo_rejects_invalid_complete_list_items() -> None:
    tool = create_todo_tool()

    missing_content = tool.executor(todos=[{"status": "pending", "priority": "medium"}])
    invalid_status = tool.executor(todos=[{"content": "任务", "status": "working"}])
    missing_status = tool.executor(todos=[{"content": "任务", "priority": "urgent"}])

    assert missing_content.ok is False
    assert "缺少 content" in missing_content.error
    assert invalid_status.ok is False
    assert "未知状态" in invalid_status.error
    assert missing_status.ok is True


def test_todo_definition_accepts_only_complete_todo_list() -> None:
    tool = create_todo_tool()

    assert tool.name == "todo"
    parameters = tool.definition.parameters
    assert parameters["required"] == ["todos"]
    assert set(parameters["properties"]) == {"todos"}
    todo_items = parameters["properties"]["todos"]["items"]
    assert todo_items["required"] == ["content", "status"]
    assert set(todo_items["properties"]) == {"content", "status"}
    assert todo_items["properties"]["status"]["enum"] == [
        "pending",
        "in_progress",
        "completed",
        "cancelled",
    ]
    assert tool.definition.description == (
        "Replace the current Todo list for multi-step work. Each item has content and status; "
        "at most one item may be in_progress. An empty list clears Todo state."
    )


def test_todo_description_only_states_tool_semantics() -> None:
    description = create_todo_tool().definition.description

    assert "Replace the current Todo list" in description
    assert "at most one item may be in_progress" in description
    assert "An empty list clears Todo state" in description
    assert "3-7 item plan" not in description
    assert "Before a final answer" not in description
    assert "routine status updates" not in description
