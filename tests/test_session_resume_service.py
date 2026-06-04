from pathlib import Path

import pytest

from firstcoder.agent.session import AgentSession
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.providers.types import ToolCall
from firstcoder.session.errors import SessionCorruptError, SessionEmptyError, SessionNotFoundError
from firstcoder.session.resume import ResumeService
from firstcoder.tools.write import create_write_tool


def test_resume_service_resumes_existing_session_and_reads_agents_md(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("项目规则", encoding="utf-8")
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    writer.append_session_created(title="demo")
    writer.append_user_message("历史消息")

    result = ResumeService(store=store, project_root=tmp_path).resume("sess_test")

    assert result.record.session_id == "sess_test"
    assert result.record.title == "demo"
    assert result.session.session_id == "sess_test"
    assert result.session.agents_md == "项目规则"
    assert result.session.turn_counter == 1
    assert result.session.current_turn == 1


def test_resume_service_replays_runtime_state_and_known_message_ids(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    original = AgentSession.create(store=store, session_id="sess_test", agents_md="")
    message_id = original.append_user_message("新任务")
    tool_call = ToolCall(
        id="call_boundary",
        name="task_boundary",
        arguments={"decision": "new", "basis_message_id": message_id},
    )
    first = original.execute_tool_call(tool_call)
    original.append_tool_result(tool_call=tool_call, result=first)
    second = original.execute_tool_call(tool_call)
    original.append_tool_result(tool_call=tool_call, result=second)

    result = ResumeService(store=store, project_root=tmp_path).resume("sess_test")
    boundary_result = result.session.tool_registry.execute(
        "task_boundary",
        {"decision": "same", "basis_message_id": message_id},
    )

    assert result.session.runtime_state.active_task_hash == original.runtime_state.active_task_hash
    assert message_id in result.session.known_message_ids
    assert boundary_result.ok is True


def test_resume_service_keeps_permission_wrapper_for_project_tools(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    original = AgentSession.from_project(
        store=store,
        session_id="sess_permissions",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
    original.append_user_message("历史消息")

    result = ResumeService(
        store=store,
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    ).resume("sess_permissions")
    tool_result = result.session.execute_tool_call(
        ToolCall(
            id="call_write",
            name="write",
            arguments={"path": "README.md", "content": "hello"},
        )
    )

    assert tool_result.ok is True
    assert tool_result.data["request_type"] == "permission_confirmation"
    assert tool_result.data["permission_request"]["action"] == "write_path"
    assert not (tmp_path / "README.md").exists()


def test_resume_service_rejects_missing_session(tmp_path: Path) -> None:
    service = ResumeService(store=JsonlSessionStore(tmp_path), project_root=tmp_path)

    with pytest.raises(SessionNotFoundError):
        service.resume("sess_missing")


def test_resume_service_rejects_corrupt_session(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    (tmp_path / "sessions" / "sess_corrupt.jsonl").write_text("{not json}\n", encoding="utf-8")

    service = ResumeService(store=store, project_root=tmp_path)

    with pytest.raises(SessionCorruptError):
        service.resume("sess_corrupt")


def test_resume_service_rejects_empty_session(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    (tmp_path / "sessions" / "sess_empty.jsonl").write_text("", encoding="utf-8")

    service = ResumeService(store=store, project_root=tmp_path)

    with pytest.raises(SessionEmptyError):
        service.resume("sess_empty")
