from firstcoder.context.checkpoint import Checkpoint
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.providers.types import ChatMessage, ToolDefinition
from firstcoder.context.token_budget import estimate_chat_request_tokens
from firstcoder.context.triggers import ContextCompactionConfig, evaluate_context_triggers


def _message(
    message_id: str,
    content: str,
    *,
    role: str = "user",
    kind: str = "text",
    metadata: dict[str, object] | None = None,
) -> AgentMessage:
    return AgentMessage(
        id=message_id,
        session_id="sess_test",
        role=role,
        parts=[
            MessagePart(
                id=f"part_{message_id}",
                message_id=message_id,
                kind=kind,
                content=content,
                metadata=metadata or {},
            )
        ],
    )


def test_token_thresholds_trigger_expected_compaction_reason() -> None:
    config = ContextCompactionConfig()
    view = SessionView(session_id="sess_test", messages=[_message("msg_1", "x" * 80)])

    decision = evaluate_context_triggers(
        view,
        config,
        input_tokens=10,
        high_watermark=10,
        low_watermark=5,
    )

    assert decision.should_compact is True
    assert decision.reason == "token_threshold"
    assert decision.target_tokens == 5


def test_request_token_estimate_includes_system_messages_tools_and_reserved_output() -> None:
    estimate = estimate_chat_request_tokens(
        messages=[
            ChatMessage(role="system", content="system" * 40),
            ChatMessage(role="user", content="user" * 40),
        ],
        tools=[
            ToolDefinition(
                name="view",
                description="read a file" * 20,
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            )
        ],
        reserved_output_tokens=50,
    )

    assert estimate >= 140


def test_request_token_estimate_reserves_requested_output_space() -> None:
    baseline = estimate_chat_request_tokens(
        messages=[ChatMessage(role="user", content="hello")],
        tools=[],
    )
    reserved = estimate_chat_request_tokens(
        messages=[ChatMessage(role="user", content="hello")],
        tools=[],
        reserved_output_tokens=512,
    )

    assert reserved == baseline + 512


def test_large_single_tool_result_does_not_bypass_dynamic_watermark() -> None:
    config = ContextCompactionConfig(
        large_tool_result_tokens=5,
    )
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_tool",
                "large tool output" * 20,
                role="tool",
                kind="tool_result",
                metadata={"tool_call_id": "call_1", "tool_name": "shell"},
            )
        ],
    )

    decision = evaluate_context_triggers(
        view,
        config,
        input_tokens=20,
        high_watermark=100,
        low_watermark=60,
    )

    assert decision.should_compact is False
    assert decision.reason == "under_threshold"


def test_large_turn_tool_results_do_not_bypass_dynamic_watermark() -> None:
    config = ContextCompactionConfig(
        max_turn_tool_result_tokens=10,
        large_tool_result_tokens=10_000,
    )
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_tool_1",
                "tool output" * 10,
                role="tool",
                kind="tool_result",
                metadata={"tool_call_id": "call_1", "tool_name": "shell", "turn_id": "turn_1"},
            ),
            _message(
                "msg_tool_2",
                "tool output" * 10,
                role="tool",
                kind="tool_result",
                metadata={"tool_call_id": "call_2", "tool_name": "shell", "turn_id": "turn_1"},
            ),
        ],
    )

    decision = evaluate_context_triggers(
        view,
        config,
        input_tokens=20,
        high_watermark=100,
        low_watermark=60,
    )

    assert decision.should_compact is False
    assert decision.reason == "under_threshold"


def test_tail_message_count_does_not_bypass_dynamic_watermark() -> None:
    config = ContextCompactionConfig(
        max_tail_messages=2,
    )
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_1", "a"),
            _message("msg_2", "b"),
            _message("msg_3", "c"),
        ],
    )

    decision = evaluate_context_triggers(
        view,
        config,
        input_tokens=3,
        high_watermark=100,
        low_watermark=60,
    )

    assert decision.should_compact is False
    assert decision.reason == "under_threshold"


def test_checkpointed_history_is_excluded_from_tail_message_trigger() -> None:
    config = ContextCompactionConfig(
        max_tail_messages=1,
    )
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_1", "old 1"),
            _message("msg_2", "old 2"),
            _message("msg_3", "tail"),
        ],
        checkpoints=[
            Checkpoint(
                id="ckpt_1",
                session_id="sess_test",
                summary="old summary",
                tail_start_message_id="msg_3",
                covered_until_message_id="msg_2",
                source_fingerprint="fp_1",
                sequence=1,
            )
        ],
    )

    decision = evaluate_context_triggers(
        view,
        config,
        input_tokens=3,
        high_watermark=100,
        low_watermark=60,
    )

    assert decision.should_compact is False
    assert decision.reason == "under_threshold"


def test_checkpointed_history_is_excluded_from_token_threshold_but_summary_counts() -> None:
    config = ContextCompactionConfig()
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_1", "old raw content" * 200),
            _message("msg_2", "tail"),
        ],
        checkpoints=[
            Checkpoint(
                id="ckpt_1",
                session_id="sess_test",
                summary="short summary",
                tail_start_message_id="msg_2",
                covered_until_message_id="msg_1",
                source_fingerprint="fp_1",
                sequence=1,
            )
        ],
    )

    decision = evaluate_context_triggers(
        view,
        config,
        input_tokens=9,
        high_watermark=10,
        low_watermark=5,
    )

    assert decision.should_compact is False
    assert decision.reason == "under_threshold"
    assert decision.estimated_tokens == 9


def test_checkpointed_large_tool_result_is_excluded_from_archive_guard() -> None:
    config = ContextCompactionConfig(
        large_tool_result_tokens=5,
    )
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_tool_old",
                "large old tool output" * 50,
                role="tool",
                kind="tool_result",
                metadata={"tool_call_id": "call_old", "tool_name": "shell"},
            ),
            _message("msg_tail", "tail"),
        ],
        checkpoints=[
            Checkpoint(
                id="ckpt_1",
                session_id="sess_test",
                summary="tool summary",
                tail_start_message_id="msg_tail",
                covered_until_message_id="msg_tool_old",
                source_fingerprint="fp_1",
                sequence=1,
            )
        ],
    )

    decision = evaluate_context_triggers(
        view,
        config,
        input_tokens=3,
        high_watermark=100,
        low_watermark=60,
    )

    assert decision.should_compact is False
    assert decision.reason == "under_threshold"
