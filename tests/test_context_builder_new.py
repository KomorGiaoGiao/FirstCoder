import pytest

from firstcoder.context.context_builder import ContextBuilder
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.tool_sequence import InvalidToolCallSequenceError, validate_tool_call_sequence
from firstcoder.context.system_prompt import SystemPromptBuilder, SystemPromptInputs
from firstcoder.providers.types import ChatMessage


def test_context_builder_projects_internal_messages_to_provider_messages() -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            AgentMessage(
                id="msg_user",
                session_id="sess_test",
                role="user",
                parts=[
                    MessagePart(
                        id="part_user",
                        message_id="msg_user",
                        kind="text",
                        content="读一下 README",
                    )
                ],
                created_at="2026-06-01T00:00:00Z",
            ),
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
                        metadata={
                            "tool_call_id": "call_1",
                            "tool_name": "read_file",
                            "arguments": {"path": "README.md"},
                        },
                    )
                ],
                created_at="2026-06-01T00:00:01Z",
            ),
            AgentMessage(
                id="msg_tool",
                session_id="sess_test",
                role="tool",
                parts=[
                    MessagePart(
                        id="part_result",
                        message_id="msg_tool",
                        kind="tool_result",
                        content="README 内容预览",
                        metadata={"tool_call_id": "call_1", "tool_name": "read_file"},
                    )
                ],
                created_at="2026-06-01T00:00:02Z",
            ),
        ],
    )

    messages = ContextBuilder().build_provider_messages(
        view,
        system_prefix=[ChatMessage(role="system", content="你是 FirstCoder。")],
    )

    assert [message.role for message in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert "basis_message_id=msg_user" in messages[1].content
    assert "读一下 README" in messages[1].content
    assert messages[2].tool_calls[0].name == "read_file"
    assert messages[3].tool_call_id == "call_1"
    assert messages[3].content == "README 内容预览"


def test_context_builder_filters_system_meta_messages() -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            AgentMessage(
                id="msg_meta",
                session_id="sess_test",
                role="system_meta",
                parts=[
                    MessagePart(
                        id="part_meta",
                        message_id="msg_meta",
                        kind="compaction_event_ref",
                        content="内部事件",
                    )
                ],
            )
        ],
    )

    assert ContextBuilder().build_provider_messages(view) == []


def test_context_builder_projects_tool_archive_placeholder_as_tool_message() -> None:
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
                        metadata={"tool_call_id": "call_1", "tool_name": "shell", "arguments": {}},
                    )
                ],
            ),
            AgentMessage(
                id="msg_tool",
                session_id="sess_test",
                role="tool",
                parts=[
                    MessagePart(
                        id="part_archive",
                        message_id="msg_tool",
                        kind="archive_placeholder",
                        content="[Tool result archived]\narchive_id=ar_1",
                        metadata={"tool_call_id": "call_1", "tool_name": "shell"},
                    )
                ],
            ),
        ],
    )

    messages = ContextBuilder().build_provider_messages(view)

    assert len(messages) == 2
    assert messages[0].role == "assistant"
    assert messages[1].role == "tool"
    assert messages[1].content == "[Tool result archived]\narchive_id=ar_1"
    assert messages[1].tool_call_id == "call_1"


def test_context_builder_accepts_stable_system_prefix_from_builder() -> None:
    prefix = SystemPromptBuilder().build(
        SystemPromptInputs(
            base_rules="你是 FirstCoder。",
            agents_md="上下文管理放在 firstcoder/context。",
            provider_name="test-provider",
            provider_capabilities={"tool_calling": True},
            permission_policy={"read": "allow"},
        )
    )
    view = SessionView(
        session_id="sess_test",
        messages=[
            AgentMessage(
                id="msg_user",
                session_id="sess_test",
                role="user",
                parts=[
                    MessagePart(
                        id="part_user",
                        message_id="msg_user",
                        kind="text",
                        content="继续实现上下文。",
                    )
                ],
            )
        ],
    )

    messages = ContextBuilder().build_provider_messages(view, system_prefix=prefix.messages)

    assert [message.role for message in messages] == ["system", "user"]
    assert "你是 FirstCoder。" in messages[0].content
    assert "basis_message_id=msg_user" in messages[1].content
    assert "继续实现上下文。" in messages[1].content


def test_context_builder_projects_one_trim_marker_and_preserves_latest_user_and_tool_chain() -> None:
    """L1's empty parts never make an orphan or blank provider message."""

    trimmed_metadata = {"task_hash": "old", "compaction_state": "trimmed"}
    view = SessionView(
        session_id="sess_test",
        messages=[
            AgentMessage(
                id="msg_old_user",
                session_id="sess_test",
                role="user",
                parts=[
                    MessagePart(
                        id="part_old_user",
                        message_id="msg_old_user",
                        kind="text",
                        content="raw old user text",
                        metadata=trimmed_metadata,
                    )
                ],
            ),
            AgentMessage(
                id="msg_old_assistant",
                session_id="sess_test",
                role="assistant",
                parts=[
                    MessagePart(
                        id="part_old_assistant",
                        message_id="msg_old_assistant",
                        kind="text",
                        content="raw old assistant text",
                        metadata=trimmed_metadata,
                    )
                ],
            ),
            AgentMessage(
                id="msg_tool_call",
                session_id="sess_test",
                role="assistant",
                parts=[
                    MessagePart(
                        id="part_tool_call_text",
                        message_id="msg_tool_call",
                        kind="text",
                        content="Keep this tool rationale.",
                        metadata=trimmed_metadata,
                    ),
                    MessagePart(
                        id="part_tool_call",
                        message_id="msg_tool_call",
                        kind="tool_call",
                        content="",
                        metadata={"tool_call_id": "call_1", "tool_name": "shell", "arguments": {}},
                    ),
                ],
            ),
            AgentMessage(
                id="msg_tool_result",
                session_id="sess_test",
                role="tool",
                parts=[
                    MessagePart(
                        id="part_tool_result",
                        message_id="msg_tool_result",
                        kind="tool_result",
                        content="tool result",
                        metadata={"tool_call_id": "call_1", "tool_name": "shell"},
                    )
                ],
            ),
            AgentMessage(
                id="msg_latest_user",
                session_id="sess_test",
                role="user",
                parts=[
                    MessagePart(
                        id="part_latest_user",
                        message_id="msg_latest_user",
                        kind="text",
                        content="latest user requirement",
                        metadata=trimmed_metadata,
                    )
                ],
            ),
        ],
    )

    projected = ContextBuilder().build_provider_messages(view)

    assert [message.role for message in projected] == ["user", "assistant", "tool", "user"]
    assert projected[0].content == "[Earlier dialogue trimmed]"
    assert projected[1].content == "Keep this tool rationale."
    assert projected[1].tool_calls[0].id == "call_1"
    assert projected[2].tool_call_id == "call_1"
    assert "latest user requirement" in projected[3].content
    assert sum(message.content == "[Earlier dialogue trimmed]" for message in projected) == 1
    validate_tool_call_sequence(view.messages)


def test_context_builder_collapses_identical_adjacent_duplicate_tool_call_before_result() -> None:
    arguments = {"path": "app.py", "old": "old", "new": "new"}
    view = SessionView(
        session_id="sess_duplicate_call",
        messages=[
            AgentMessage(
                id="msg_first",
                session_id="sess_duplicate_call",
                role="assistant",
                parts=[
                    MessagePart(
                        id="part_first",
                        message_id="msg_first",
                        kind="tool_call",
                        content="",
                        metadata={
                            "tool_call_id": "call_duplicate",
                            "tool_name": "edit",
                            "arguments": arguments,
                            "prewrite_review_only": True,
                        },
                    )
                ],
            ),
            AgentMessage(
                id="msg_second",
                session_id="sess_duplicate_call",
                role="assistant",
                parts=[
                    MessagePart(
                        id="part_second",
                        message_id="msg_second",
                        kind="tool_call",
                        content="",
                        metadata={
                            "tool_call_id": "call_duplicate",
                            "tool_name": "edit",
                            "arguments": arguments,
                        },
                    )
                ],
            ),
            AgentMessage(
                id="msg_result",
                session_id="sess_duplicate_call",
                role="tool",
                parts=[
                    MessagePart(
                        id="part_result",
                        message_id="msg_result",
                        kind="tool_result",
                        content="edited",
                        metadata={"tool_call_id": "call_duplicate", "tool_name": "edit"},
                    )
                ],
            ),
        ],
    )

    projected = ContextBuilder().build_provider_messages(view)

    assert [message.role for message in projected] == ["assistant", "tool"]
    assert projected[0].tool_calls[0].id == "call_duplicate"
    assert projected[1].tool_call_id == "call_duplicate"


def test_context_builder_does_not_collapse_duplicate_id_when_arguments_differ() -> None:
    messages = []
    for suffix, new_value in (("first", "one"), ("second", "two")):
        messages.append(
            AgentMessage(
                id=f"msg_{suffix}",
                session_id="sess_conflicting_duplicate",
                role="assistant",
                parts=[
                    MessagePart(
                        id=f"part_{suffix}",
                        message_id=f"msg_{suffix}",
                        kind="tool_call",
                        content="",
                        metadata={
                            "tool_call_id": "call_same_id",
                            "tool_name": "edit",
                            "arguments": {"path": "app.py", "old": "old", "new": new_value},
                        },
                    )
                ],
            )
        )
    messages.append(
        AgentMessage(
            id="msg_result",
            session_id="sess_conflicting_duplicate",
            role="tool",
            parts=[
                MessagePart(
                    id="part_result",
                    message_id="msg_result",
                    kind="tool_result",
                    content="edited",
                    metadata={"tool_call_id": "call_same_id", "tool_name": "edit"},
                )
            ],
        )
    )

    with pytest.raises(InvalidToolCallSequenceError, match="missing matching tool result"):
        ContextBuilder().build_provider_messages(SessionView(session_id="sess_conflicting_duplicate", messages=messages))
