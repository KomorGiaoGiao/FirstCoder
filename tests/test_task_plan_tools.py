from __future__ import annotations

import pytest

from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.planning.service import TaskPlanService
from firstcoder.tools.task_create import create_task_create_tool
from firstcoder.tools.task_list import create_task_list_tool
from firstcoder.tools.task_revise import create_task_revise_tool
from firstcoder.tools.task_update import create_task_update_tool


def _tools(tmp_path):
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_plan")
    writer.append_session_created()
    service = TaskPlanService(store=store, writer=writer)
    return {
        tool.name: tool
        for tool in (
            create_task_create_tool(service),
            create_task_update_tool(service),
            create_task_revise_tool(service),
            create_task_list_tool(service),
        )
    }


def test_task_plan_tool_schemas_are_incremental_and_exact(tmp_path) -> None:
    tools = _tools(tmp_path)
    assert set(tools) == {"task_create", "task_update", "task_revise", "task_list"}
    assert tools["task_create"].definition.parameters["required"] == [
        "mode",
        "expected_revision",
        "tasks",
    ]
    assert tools["task_update"].definition.parameters["required"] == [
        "expected_revision",
        "updates",
    ]
    assert tools["task_revise"].definition.parameters["required"] == [
        "expected_revision",
        "revisions",
    ]
    assert "required" not in tools["task_list"].definition.parameters

    update_item = tools["task_update"].definition.parameters["properties"]["updates"]["items"]
    assert "content" not in update_item["properties"]
    for tool in tools.values():
        assert tool.definition.parameters["additionalProperties"] is False
        properties = tool.definition.parameters["properties"]
        assert "task_plan" not in properties
        assert "ready_nodes" not in properties


def test_task_plan_tools_return_normalized_mutations_and_current_state(tmp_path) -> None:
    tools = _tools(tmp_path)
    created = tools["task_create"].executor(
        mode="linear",
        expected_revision=0,
        tasks=[{"id": "inspect", "content": "Inspect"}],
    )
    updated = tools["task_update"].executor(
        expected_revision=1,
        updates=[{"id": "inspect", "owner": "main", "status": "in_progress"}],
    )
    revised = tools["task_revise"].executor(
        expected_revision=2,
        revisions=[{"id": "inspect", "content": "Inspect carefully"}],
    )
    listed = tools["task_list"].executor()

    assert created.ok and created.data["revision"] == 1
    assert created.data["changes"][0]["order"] == 0
    assert updated.ok and updated.data["changes"] == [{"id": "inspect", "status": "in_progress", "owner": "main"}]
    assert revised.ok and revised.data["changes"] == [{"id": "inspect", "content": "Inspect carefully"}]
    assert revised.data["snapshot"] == listed.data["plan"]
    assert "plan" not in revised.data
    assert listed.data["revision"] == 3
    assert listed.data["projection"]["ready_task_ids"] == []


def test_task_list_without_plan_returns_explicit_empty_state(tmp_path) -> None:
    result = _tools(tmp_path)["task_list"].executor()

    assert result.ok is True
    assert result.data == {"revision": 0, "plan": None, "projection": None}


def test_revision_conflict_is_recoverable_and_actionable(tmp_path) -> None:
    tools = _tools(tmp_path)
    tools["task_create"].executor(
        mode="linear",
        expected_revision=0,
        tasks=[{"id": "work", "content": "Work"}],
    )

    result = tools["task_update"].executor(
        expected_revision=0,
        updates=[{"id": "work", "status": "in_progress"}],
    )

    assert result.ok is False
    assert result.data["actual_revision"] == 1
    assert "task_list" in result.content


@pytest.mark.parametrize("tool_name", ["task_create", "task_update", "task_revise"])
def test_validation_errors_are_recoverable_and_do_not_retry(tmp_path, tool_name: str) -> None:
    tools = _tools(tmp_path)
    if tool_name != "task_create":
        tools["task_create"].executor(
            mode="dag",
            expected_revision=0,
            tasks=[{"id": "work", "content": "Work"}],
        )
    arguments = {
        "task_create": {
            "mode": "dag",
            "expected_revision": 0,
            "tasks": [{"id": "work", "content": "Work", "depends_on": ["missing"]}],
        },
        "task_update": {
            "expected_revision": 1,
            "updates": [{"id": "work", "add_depends_on": ["missing"]}],
        },
        "task_revise": {
            "expected_revision": 1,
            "revisions": [{"id": "work", "content": "   "}],
        },
    }[tool_name]

    result = tools[tool_name].executor(**arguments)

    assert result.ok is False
    assert result.error
