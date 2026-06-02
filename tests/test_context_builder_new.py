from firstcoder.context.context_builder import ContextBuilder
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
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
    assert messages[1].content == "读一下 README"
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
            )
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
            tools=[],
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
    assert messages[1].content == "继续实现上下文。"
