from pathlib import Path

import pytest

from firstcoder.agent.session import AgentSession
from firstcoder.agent.loop import AgentLoop
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse, ToolCall
from firstcoder.session.errors import SessionCorruptError, SessionEmptyError, SessionNotFoundError
from firstcoder.session.resume import ResumeService
from firstcoder.tools.write import create_write_tool


class FakeProvider(ChatProvider):
    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = responses
        self.requests: list[ChatRequest] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return self.responses.pop(0)


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


def test_resume_service_rediscovers_current_project_skill_catalog(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "AGENTS.md").write_text("项目规则", encoding="utf-8")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "brief.md").write_text("# Brief\n\n写简报。", encoding="utf-8")
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    writer = SessionEventWriter(store=store, session_id="sess_skills")
    writer.append_session_created(title="demo")

    result = ResumeService(store=store, project_root=tmp_path).resume("sess_skills")

    assert [skill.path for skill in result.session.skill_catalog.skills] == ["skills/brief.md"]


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


def test_resume_service_restores_pending_permission_confirmation(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    original = AgentSession.from_project(
        store=store,
        session_id="sess_pending_permission",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_write",
                        name="write",
                        arguments={"path": "README.md", "content": "hello"},
                    )
                ],
                finish_reason="tool_calls",
            )
        ]
    )

    pending = AgentLoop(session=original, provider=provider).run_user_turn_interactive("写 README")
    assert pending.pending_input is not None
    result = ResumeService(
        store=store,
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        tools=[create_write_tool(tmp_path)],
    ).resume("sess_pending_permission")

    assert result.session.pending_permission_execution is not None
    assert result.session.pending_permission_execution.tool_call.id == "call_write"


def test_resume_service_restores_pending_permission_even_after_grant_exists(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    original = AgentSession.from_project(
        store=store,
        session_id="sess_pending_with_grant",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_write",
                        name="write",
                        arguments={"path": "README.md", "content": "hello"},
                    )
                ],
                finish_reason="tool_calls",
            )
        ]
    )

    pending = AgentLoop(session=original, provider=provider).run_user_turn_interactive("写 README")
    assert pending.pending_input is not None
    original.permission_manager.resolve_confirmation(
        original.pending_permission_execution.permission_request,
        "allow_always_same_scope",
    )

    result = ResumeService(
        store=store,
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        tools=[create_write_tool(tmp_path)],
    ).resume("sess_pending_with_grant")

    assert result.session.pending_permission_execution is not None
    assert result.session.pending_permission_execution.request_id == pending.pending_input.id


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
