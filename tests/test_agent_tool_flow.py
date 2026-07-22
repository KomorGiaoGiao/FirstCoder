from __future__ import annotations

import pytest

from firstcoder.agent.session import AgentSession
from firstcoder.agent.tool_flow import (
    InvalidToolCallSequenceError,
    assistant_response_to_parts,
    tool_call_to_part,
    tool_result_to_part,
    validate_tool_call_sequence,
)
from firstcoder.context.context_builder import ContextBuilder
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter, tool_call_to_part as writer_tool_call_to_part
from firstcoder.providers.types import ChatResponse, ToolCall
from firstcoder.tools.apply_patch import create_apply_patch_tool
from firstcoder.tools.python_exec import create_python_exec_tool
from firstcoder.tools.session_registry import create_session_tool_registry
from firstcoder.tools.write import create_write_tool
from firstcoder.tools.types import ToolResult


def test_agent_reexports_context_tool_call_conversion() -> None:
    assert tool_call_to_part is writer_tool_call_to_part


def test_assistant_tool_calls_are_persisted_as_tool_call_parts(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    response = ChatResponse(
        provider="fake",
        model="fake-model",
        content="需要读取文件",
        tool_calls=[ToolCall(id="call_1", name="read_file", arguments={"path": "a.py"})],
        finish_reason="tool_calls",
    )

    message_id = writer.append_assistant_parts(
        assistant_response_to_parts(message_id="msg_assistant", response=response),
        metadata={"provider": response.provider, "model": response.model},
        message_id="msg_assistant",
    )

    view = store.rebuild_session_view("sess_test")
    assert message_id == "msg_assistant"
    assert view.messages[0].role == "assistant"
    assert [part.kind for part in view.messages[0].parts] == ["text", "tool_call"]
    assert view.messages[0].parts[1].metadata["tool_call_id"] == "call_1"
    assert view.messages[0].parts[1].metadata["tool_name"] == "read_file"
    assert view.messages[0].parts[1].metadata["arguments"] == {"path": "a.py"}
    assert view.messages[0].parts[1].metadata["created_turn"] == 0
    assert view.messages[0].parts[1].metadata["turn_id"] == 0


def test_tool_results_are_persisted_with_matching_tool_call_id(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    tool_call = ToolCall(id="call_1", name="grep", arguments={"pattern": "TODO"})
    result = ToolResult(name="grep", ok=True, content="found", data={"count": 1})

    writer.append_tool_result_part(
        tool_result_to_part(message_id="msg_tool", tool_call=tool_call, result=result),
        message_id="msg_tool",
    )

    part = store.rebuild_session_view("sess_test").messages[0].parts[0]
    assert part.kind == "tool_result"
    assert part.content == "found"
    assert part.metadata["tool_call_id"] == "call_1"
    assert part.metadata["tool_name"] == "grep"
    assert part.metadata["ok"] is True
    assert part.metadata["data"] == {"count": 1}


def test_unknown_tool_result_is_persisted_as_structured_error(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_test")
    tool_call = ToolCall(id="call_missing", name="missing_tool", arguments={"x": 1})

    result = session.execute_tool_call(tool_call)
    session.append_tool_result(tool_call=tool_call, result=result)

    part = store.rebuild_session_view("sess_test").messages[0].parts[0]
    assert result.ok is False
    assert part.kind == "tool_result"
    assert part.metadata["tool_call_id"] == "call_missing"
    assert part.metadata["tool_name"] == "missing_tool"
    assert part.metadata["ok"] is False
    assert "未知工具" in part.metadata["error"]


def test_project_session_permissioned_write_pauses_without_writing(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_permissions",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
    tool_call = ToolCall(
        id="call_write",
        name="write",
        arguments={"path": "README.md", "content": "hello"},
    )

    result = session.execute_tool_call(tool_call)

    assert result.ok is True
    assert result.data["requires_user_input"] is True
    assert result.data["request_type"] == "permission_confirmation"
    assert result.data["permission_request"]["action"] == "write_path"
    assert result.data["permission_request"]["cwd"] == str(tmp_path.resolve())
    assert not (tmp_path / "README.md").exists()


def test_project_session_permissioned_apply_patch_pauses_without_writing(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_patch_permissions",
        project_root=tmp_path,
        tools=[create_apply_patch_tool(tmp_path)],
    )
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: created.txt",
            "+hello",
            "*** End Patch",
        ]
    )

    result = session.execute_tool_call(
        ToolCall(
            id="call_patch",
            name="apply_patch",
            arguments={"patch": patch},
        )
    )

    assert result.ok is True
    assert result.data["request_type"] == "permission_confirmation"
    assert result.data["permission_request"]["action"] == "write_path"
    assert result.data["permission_request"]["target"] == "created.txt"
    assert [option["id"] for option in result.data["options"]] == ["deny", "allow_once"]
    assert not (tmp_path / "created.txt").exists()


def test_project_session_permissioned_python_exec_pauses_without_executing(tmp_path, monkeypatch) -> None:
    from firstcoder.tools import python_exec as python_exec_module

    called = False

    def fake_run(command, **kwargs):
        nonlocal called
        called = True
        return python_exec_module.subprocess.CompletedProcess(command, 0, "42\n", "")

    monkeypatch.setattr(python_exec_module.subprocess, "run", fake_run)
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_python_permissions",
        project_root=tmp_path,
        tools=[create_python_exec_tool(tmp_path)],
    )

    result = session.execute_tool_call(
        ToolCall(
            id="call_python",
            name="python_exec",
            arguments={"code": "print(42)"},
        )
    )

    assert result.ok is True
    assert result.data["request_type"] == "permission_confirmation"
    assert result.data["permission_request"]["action"] == "execute_shell"
    assert [option["id"] for option in result.data["options"]] == ["deny", "allow_once"]
    assert called is False


def test_session_registry_adds_task_boundary_tool() -> None:
    registry = create_session_tool_registry(session_id="sess_test")

    assert "task_boundary" in registry.names()
    result = registry.execute("task_boundary", {"decision": "new", "basis_message_id": "msg_1"})
    assert result.ok is True
    assert result.data["candidate_hash"].startswith("task_")


def test_context_builder_rejects_orphan_tool_result() -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            AgentMessage(
                id="msg_user",
                session_id="sess_test",
                role="user",
                parts=[MessagePart(id="part_user", message_id="msg_user", kind="text", content="继续")],
            ),
            AgentMessage(
                id="msg_tool",
                session_id="sess_test",
                role="tool",
                parts=[
                    MessagePart(
                        id="part_tool",
                        message_id="msg_tool",
                        kind="tool_result",
                        content="result",
                        metadata={"tool_call_id": "call_orphan", "tool_name": "grep"},
                    )
                ],
            ),
        ],
    )

    with pytest.raises(InvalidToolCallSequenceError):
        validate_tool_call_sequence(view.messages)
    with pytest.raises(InvalidToolCallSequenceError):
        ContextBuilder().build_provider_messages(view)


def test_context_builder_rejects_assistant_tool_call_without_tool_result() -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            AgentMessage(
                id="msg_assistant",
                session_id="sess_test",
                role="assistant",
                parts=[
                    MessagePart(
                        id="part_call",
                        message_id="msg_assistant",
                        kind="tool_call",
                        content="",
                        metadata={"tool_call_id": "call_1", "tool_name": "grep", "arguments": {}},
                    )
                ],
            ),
            AgentMessage(
                id="msg_user",
                session_id="sess_test",
                role="user",
                parts=[MessagePart(id="part_user", message_id="msg_user", kind="text", content="继续")],
            ),
        ],
    )

    with pytest.raises(InvalidToolCallSequenceError):
        validate_tool_call_sequence(view.messages)
    with pytest.raises(InvalidToolCallSequenceError):
        ContextBuilder().build_provider_messages(view)


def test_context_builder_rejects_history_ending_with_pending_tool_call() -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            AgentMessage(
                id="msg_assistant",
                session_id="sess_test",
                role="assistant",
                parts=[
                    MessagePart(
                        id="part_call",
                        message_id="msg_assistant",
                        kind="tool_call",
                        content="",
                        metadata={"tool_call_id": "call_1", "tool_name": "grep", "arguments": {}},
                    )
                ],
            )
        ],
    )

    with pytest.raises(InvalidToolCallSequenceError):
        validate_tool_call_sequence(view.messages)
    with pytest.raises(InvalidToolCallSequenceError):
        ContextBuilder().build_provider_messages(view)


def test_context_builder_accepts_parallel_tool_calls_split_across_tool_messages() -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            AgentMessage(
                id="msg_assistant",
                session_id="sess_test",
                role="assistant",
                parts=[
                    MessagePart(
                        id="part_call_1",
                        message_id="msg_assistant",
                        kind="tool_call",
                        content="",
                        metadata={"tool_call_id": "call_1", "tool_name": "grep", "arguments": {"pattern": "TODO"}},
                    ),
                    MessagePart(
                        id="part_call_2",
                        message_id="msg_assistant",
                        kind="tool_call",
                        content="",
                        metadata={"tool_call_id": "call_2", "tool_name": "read_file", "arguments": {"path": "a.py"}},
                    ),
                ],
            ),
            AgentMessage(
                id="msg_tool_1",
                session_id="sess_test",
                role="tool",
                parts=[
                    MessagePart(
                        id="part_result_1",
                        message_id="msg_tool_1",
                        kind="tool_result",
                        content="found",
                        metadata={"tool_call_id": "call_1", "tool_name": "grep"},
                    )
                ],
            ),
            AgentMessage(
                id="msg_tool_2",
                session_id="sess_test",
                role="tool",
                parts=[
                    MessagePart(
                        id="part_result_2",
                        message_id="msg_tool_2",
                        kind="tool_result",
                        content="content",
                        metadata={"tool_call_id": "call_2", "tool_name": "read_file"},
                    )
                ],
            ),
        ],
    )

    validate_tool_call_sequence(view.messages)
    messages = ContextBuilder().build_provider_messages(view)
    assert [message.role for message in messages] == ["assistant", "tool", "tool"]
    assert messages[0].tool_calls[0].id == "call_1"
    assert messages[0].tool_calls[1].id == "call_2"
    assert messages[1].tool_call_id == "call_1"
    assert messages[2].tool_call_id == "call_2"


def test_session_registry_adds_task_plan_tools_for_live_sessions(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_test", agents_md="")

    assert "task_boundary" in session.tool_registry.names()
    assert {"task_create", "task_update", "task_revise", "task_list"}.issubset(session.tool_registry.names())


def test_agent_session_task_plan_tool_writes_one_event_before_tool_result(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_task_plan_capture", agents_md="")
    tool_call = ToolCall(
        id="call_plan",
        name="task_create",
        arguments={
            "mode": "dag",
            "expected_revision": 0,
            "tasks": [
                {"id": "research", "content": "Research", "status": "completed"},
                {"id": "code", "content": "Code", "depends_on": ["research"]},
            ],
        },
    )

    result = session.tool_registry.execute(tool_call.name, tool_call.arguments)
    before_tool_result = [event for event in store.list_events("sess_task_plan_capture") if event.type == "task_plan_updated"]
    session.append_tool_result(tool_call=tool_call, result=result)
    plan_events = [event for event in store.list_events("sess_task_plan_capture") if event.type == "task_plan_updated"]
    view = store.rebuild_session_view("sess_task_plan_capture")

    assert result.ok is True
    assert len(before_tool_result) == 1
    assert len(plan_events) == 1
    assert view.task_plan is not None
    assert view.task_plan.revision == 1
    assert [task.id for task in view.task_plan.tasks] == ["research", "code"]
    assert view.messages[-1].role == "tool"
    assert view.messages[-1].parts[0].metadata["tool_name"] == "task_create"
