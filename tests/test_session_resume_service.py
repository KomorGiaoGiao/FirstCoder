from pathlib import Path

import pytest

from firstcoder.agent.session import AgentSession
from firstcoder.agent.loop import AgentLoop
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.events import SessionEvent
from firstcoder.context.writer import SessionEventWriter
from firstcoder.planning.models import Task, TaskPlan
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse, ToolCall
from firstcoder.session.errors import (
    SessionCorruptError,
    SessionEmptyError,
    SessionNotFoundError,
    SessionUnsupportedSchemaError,
)
from firstcoder.session.resume import ResumeService
from firstcoder.tools.write import create_write_tool
from firstcoder.permissions.types import PermissionMode


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


def test_resume_service_restores_session_task_plan(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_task_plan")
    writer.append_session_created(title="task plan demo")
    plan = TaskPlan(
        mode="linear",
        revision=1,
        tasks=(Task(id="restore", content="恢复任务", status="in_progress"),),
    )
    writer.append_task_plan_updated(
        previous_revision=0,
        operation="create",
        changes=[plan.tasks[0].to_dict()],
        snapshot=plan,
    )

    result = ResumeService(store=store, project_root=tmp_path).resume("sess_task_plan")

    assert result.session.rebuild_view().task_plan == plan


@pytest.mark.parametrize(
    ("schema_payload", "actual_version"),
    [
        (None, "missing"),
        ({}, "missing"),
        ({"context_event_schema_version": "v1"}, "v1"),
        ({"context_event_schema_version": "future"}, "future"),
    ],
)
def test_resume_rejects_unsupported_schema_before_bootstrap_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema_payload: dict[str, str] | None,
    actual_version: str,
) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    if schema_payload is None:
        store.append_event(
            SessionEvent(
                id="evt_metadata",
                session_id="sess_legacy",
                type="session_metadata_updated",
                payload={"title": "Legacy"},
            )
        )
    else:
        store.append_event(
            SessionEvent(
                id="evt_created",
                session_id="sess_legacy",
                type="session_created",
                payload={"session_id": "sess_legacy", **schema_payload},
            )
        )
        store.append_event(
            SessionEvent(
                id="evt_created_later",
                session_id="sess_legacy",
                type="session_created",
                payload={"context_event_schema_version": "v2"},
            )
        )
    calls: list[str] = []

    def unexpected_bootstrap(**kwargs):
        calls.append("bootstrap")
        raise AssertionError("bootstrap must not be constructed")

    monkeypatch.setattr("firstcoder.session.resume.SessionBootstrap", unexpected_bootstrap)
    monkeypatch.setattr(
        AgentSession,
        "restore_pending_permission_execution",
        lambda self: calls.append("pending_permission_restore"),
    )
    service = ResumeService(
        store=store,
        project_root=tmp_path,
        tools_provider=lambda: calls.append("tools_provider") or [],
    )

    with pytest.raises(SessionUnsupportedSchemaError) as caught:
        service.resume("sess_legacy")

    assert caught.value.session_id == "sess_legacy"
    assert caught.value.actual_version == actual_version
    assert caught.value.expected_version == "v2"
    assert "sess_legacy" in str(caught.value)
    assert actual_version in str(caught.value)
    assert "v2" in str(caught.value)
    assert calls == []


def test_resume_rejects_future_schema_before_parsing_later_events(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    path = store.sessions_dir / "sess_future.jsonl"
    path.write_text(
        '{"id":"evt_created","session_id":"sess_future","type":"session_created",' '"payload":{"context_event_schema_version":"v3"}}\n' '{"future_event_shape":true}\n',
        encoding="utf-8",
    )

    with pytest.raises(SessionUnsupportedSchemaError) as caught:
        ResumeService(store=store, project_root=tmp_path).resume("sess_future")

    assert caught.value.actual_version == "v3"


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
    assert result.session.pending_permission_execution.prewrite_review is not None
    restored_review = result.session.pending_permission_execution.prewrite_review
    assert restored_review.ok is True
    assert restored_review.files[0].path == "README.md"
    assert "+hello" in restored_review.files[0].diff
    restored_input = result.session.pending_permission_input_request()
    assert restored_input is not None
    assert "+hello" in restored_input.payload["prewrite_review"]["files"][0]["diff"]


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


def test_resume_service_has_no_pending_review_after_bypass_write(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    original = AgentSession.from_project(
        store=store,
        session_id="sess_bypass_write",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
    original.set_permission_mode(PermissionMode.BYPASS)
    tool_call = ToolCall(
        id="call_write",
        name="write",
        arguments={"path": "README.md", "content": "hello"},
    )

    tool_result = original.execute_tool_call(tool_call)

    assert tool_result.ok is True
    assert original.pending_permission_execution is None
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "hello"

    result = ResumeService(
        store=store,
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        tools=[create_write_tool(tmp_path)],
    ).resume("sess_bypass_write")

    assert result.session.pending_permission_execution is None


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


def test_resume_service_reports_invalid_utf8_session_as_corrupt(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    (tmp_path / "sessions" / "sess_invalid_utf8.jsonl").write_bytes(b"\xff\xfe\n")

    with pytest.raises(SessionCorruptError):
        ResumeService(store=store, project_root=tmp_path).resume("sess_invalid_utf8")


def test_resume_service_rejects_empty_session(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    (tmp_path / "sessions" / "sess_empty.jsonl").write_text("", encoding="utf-8")

    service = ResumeService(store=store, project_root=tmp_path)

    with pytest.raises(SessionEmptyError):
        service.resume("sess_empty")
