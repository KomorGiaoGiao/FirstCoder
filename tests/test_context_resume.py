from firstcoder.context.checkpoint import Checkpoint
from firstcoder.context.context_builder import ContextBuilder, InvalidCheckpointBoundaryError
from firstcoder.context.events import SessionEvent
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.tool_sequence import InvalidToolCallSequenceError


def _text_message(message_id: str, content: str) -> AgentMessage:
    return AgentMessage(
        id=message_id,
        session_id="sess_test",
        role="user",
        parts=[
            MessagePart(
                id=f"part_{message_id}",
                message_id=message_id,
                kind="text",
                content=content,
            )
        ],
    )


def test_context_projection_uses_checkpoint_summary_and_tail() -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _text_message("msg_1", "旧消息 1"),
            _text_message("msg_2", "旧消息 2"),
            _text_message("msg_3", "最近消息 3"),
        ],
    )
    checkpoint = Checkpoint(
        id="ckpt_1",
        session_id="sess_test",
        summary="旧历史摘要",
        tail_start_message_id="msg_3",
        covered_until_message_id="msg_2",
        source_fingerprint="source_1",
        created_at="2026-06-01T00:00:00Z",
    )

    messages = ContextBuilder().build_provider_messages(view, checkpoint=checkpoint)

    assert [message.role for message in messages] == ["user", "user"]
    assert messages[0].content == "[Checkpoint summary]\n旧历史摘要"
    assert "basis_message_id=msg_3" in messages[1].content
    assert "最近消息 3" in messages[1].content


def test_context_builder_uses_latest_checkpoint_from_rebuilt_view(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session_id = "sess_test"
    for index in range(1, 4):
        message_id = f"msg_{index}"
        store.append_event(
            SessionEvent(
                id=f"evt_{index}",
                session_id=session_id,
                type="user_message",
                payload={
                    "message_id": message_id,
                    "parts": [
                        {
                            "id": f"part_{index}",
                            "message_id": message_id,
                            "kind": "text",
                            "content": f"消息 {index}",
                        }
                    ],
                },
                created_at=f"2026-06-01T00:00:0{index}Z",
            )
        )
    store.append_event(
        SessionEvent(
            id="evt_ckpt",
            session_id=session_id,
            type="checkpoint_created",
            payload={
                "id": "ckpt_1",
                "summary": "消息 1 和消息 2 的摘要",
                "tail_start_message_id": "msg_3",
                "covered_until_message_id": "msg_2",
                "source_fingerprint": "source_1",
                "created_at": "2026-06-01T00:00:04Z",
            },
            created_at="2026-06-01T00:00:04Z",
        )
    )

    view = store.rebuild_session_view(session_id)
    messages = ContextBuilder().build_provider_messages(view)

    assert messages[0].content == "[Checkpoint summary]\n消息 1 和消息 2 的摘要"
    assert "basis_message_id=msg_3" in messages[1].content
    assert "消息 3" in messages[1].content


def test_rebuilt_view_uses_checkpoint_append_order_when_created_at_ties(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session_id = "sess_test"
    for index in range(1, 4):
        message_id = f"msg_{index}"
        store.append_event(
            SessionEvent(
                id=f"evt_msg_{index}",
                session_id=session_id,
                type="user_message",
                payload={
                    "message_id": message_id,
                    "parts": [
                        {
                            "id": f"part_{index}",
                            "message_id": message_id,
                            "kind": "text",
                            "content": f"消息 {index}",
                        }
                    ],
                },
                created_at="2026-06-01T00:00:00Z",
            )
        )
    store.append_event(
        SessionEvent(
            id="evt_ckpt_old",
            session_id=session_id,
            type="checkpoint_created",
            payload={
                "id": "ckpt_z",
                "summary": "旧 checkpoint",
                "tail_start_message_id": "msg_2",
                "covered_until_message_id": "msg_1",
                "source_fingerprint": "source_old",
                "created_at": "2026-06-01T00:00:01Z",
            },
            created_at="2026-06-01T00:00:01Z",
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_ckpt_new",
            session_id=session_id,
            type="checkpoint_created",
            payload={
                "id": "ckpt_a",
                "summary": "新 checkpoint",
                "tail_start_message_id": "msg_3",
                "covered_until_message_id": "msg_2",
                "source_fingerprint": "source_new",
                "created_at": "2026-06-01T00:00:01Z",
            },
            created_at="2026-06-01T00:00:01Z",
        )
    )

    messages = ContextBuilder().build_provider_messages(store.rebuild_session_view(session_id))

    assert messages[0].content == "[Checkpoint summary]\n新 checkpoint"
    assert "basis_message_id=msg_3" in messages[1].content
    assert "消息 3" in messages[1].content


def test_messages_before_tail_are_not_projected_twice() -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _text_message("msg_1", "已经被 checkpoint 覆盖"),
            _text_message("msg_2", "tail 起点"),
            _text_message("msg_3", "tail 后续"),
        ],
    )
    checkpoint = Checkpoint(
        id="ckpt_1",
        session_id="sess_test",
        summary="覆盖 msg_1",
        tail_start_message_id="msg_2",
        covered_until_message_id="msg_1",
        source_fingerprint="source_1",
        created_at="2026-06-01T00:00:00Z",
    )

    contents = [message.content for message in ContextBuilder().build_provider_messages(view, checkpoint=checkpoint)]

    assert "已经被 checkpoint 覆盖" not in contents
    assert contents[0] == "[Checkpoint summary]\n覆盖 msg_1"
    assert "basis_message_id=msg_2" in contents[1]
    assert "tail 起点" in contents[1]
    assert "basis_message_id=msg_3" in contents[2]
    assert "tail 后续" in contents[2]


def test_resume_does_not_expand_archived_tool_result() -> None:
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
                        metadata={"tool_name": "shell", "tool_call_id": "call_1", "arguments": {}},
                    )
                ],
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
                        content="[Tool result archived]\narchive_id=ar_1\nsummary=已归档",
                        metadata={
                            "tool_name": "shell",
                            "tool_call_id": "call_1",
                            "compaction_state": "archived",
                            "archive_id": "ar_1",
                            "archive_path": ".firstcoder/archives/sess_test/ar_1.txt",
                        },
                    )
                ],
            ),
        ],
    )

    messages = ContextBuilder().build_provider_messages(view)

    assert len(messages) == 2
    assert messages[0].role == "assistant"
    assert messages[1].role == "tool"
    assert messages[1].content.startswith("[Tool result archived]")
    assert messages[1].content != "原始工具输出"


def test_checkpoint_tail_cannot_start_with_orphan_tool_result() -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            AgentMessage(
                id="msg_tool",
                session_id="sess_test",
                role="tool",
                parts=[
                    MessagePart(
                        id="part_result",
                        message_id="msg_tool",
                        kind="tool_result",
                        content="工具结果",
                        metadata={"tool_name": "shell", "tool_call_id": "call_1"},
                    )
                ],
            )
        ],
    )
    checkpoint = Checkpoint(
        id="ckpt_1",
        session_id="sess_test",
        summary="摘要",
        tail_start_message_id="msg_tool",
        covered_until_message_id="msg_assistant",
        source_fingerprint="source_1",
        created_at="2026-06-01T00:00:00Z",
    )

    try:
        ContextBuilder().build_provider_messages(view, checkpoint=checkpoint)
    except InvalidCheckpointBoundaryError as error:
        assert "orphan tool result" in str(error)
    else:
        raise AssertionError("expected InvalidCheckpointBoundaryError")


def test_checkpoint_tail_cannot_cut_off_tool_result_after_assistant_call() -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _text_message("msg_old", "旧消息"),
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
                        metadata={"tool_name": "shell", "tool_call_id": "call_1", "arguments": {}},
                    )
                ],
            ),
        ],
    )
    checkpoint = Checkpoint(
        id="ckpt_1",
        session_id="sess_test",
        summary="摘要",
        tail_start_message_id="msg_assistant",
        covered_until_message_id="msg_old",
        source_fingerprint="source_1",
        created_at="2026-06-01T00:00:00Z",
    )

    try:
        ContextBuilder().build_provider_messages(view, checkpoint=checkpoint)
    except InvalidToolCallSequenceError as error:
        assert "assistant tool_call missing matching tool result" in str(error)
    else:
        raise AssertionError("expected invalid checkpoint tail")


def test_checkpoint_tail_start_must_exist_in_view() -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[_text_message("msg_1", "当前消息")],
    )
    checkpoint = Checkpoint(
        id="ckpt_1",
        session_id="sess_test",
        summary="摘要",
        tail_start_message_id="msg_missing",
        covered_until_message_id="msg_old",
        source_fingerprint="source_1",
        created_at="2026-06-01T00:00:00Z",
    )

    try:
        ContextBuilder().build_provider_messages(view, checkpoint=checkpoint)
    except InvalidCheckpointBoundaryError as error:
        assert "tail_start_message_id not found" in str(error)
    else:
        raise AssertionError("expected InvalidCheckpointBoundaryError")
