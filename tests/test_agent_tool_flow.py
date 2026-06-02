from __future__ import annotations

import pytest

from firstcoder.agent.session import AgentSession
from firstcoder.agent.tool_flow import (
    InvalidToolCallSequenceError,
    assistant_response_to_parts,
    tool_result_to_part,
    validate_tool_call_sequence,
)
from firstcoder.context.context_builder import ContextBuilder
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.providers.types import ChatResponse, ToolCall
from firstcoder.tools.session_registry import create_session_tool_registry
from firstcoder.tools.types import ToolResult


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
    assert view.messages[0].parts[1].metadata == {
        "tool_call_id": "call_1",
        "tool_name": "read_file",
        "arguments": {"path": "a.py"},
    }


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
