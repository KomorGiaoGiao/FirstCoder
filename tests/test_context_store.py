from pathlib import Path

from firstcoder.context.compaction import CompactionPipeline, CompactionRequest
from firstcoder.context.events import SessionEvent
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter


def test_jsonl_store_rebuilds_session_view_from_events(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session_id = "sess_test"
    user_message_id = "msg_user"
    assistant_message_id = "msg_assistant"

    store.append_event(
        SessionEvent(
            id="evt_1",
            session_id=session_id,
            type="session_created",
            payload={"title": "demo"},
            created_at="2026-06-01T00:00:00Z",
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_2",
            session_id=session_id,
            type="user_message",
            payload={
                "message_id": user_message_id,
                "parts": [
                    {
                        "id": "part_user_text",
                        "message_id": user_message_id,
                        "kind": "text",
                        "content": "实现 context store",
                        "metadata": {"task_hash": "A"},
                    }
                ],
            },
            created_at="2026-06-01T00:00:01Z",
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_3",
            session_id=session_id,
            type="assistant_message",
            payload={
                "message_id": assistant_message_id,
                "parts": [
                    MessagePart(
                        id="part_assistant_text",
                        message_id=assistant_message_id,
                        kind="text",
                        content="先写测试。",
                    ).to_dict()
                ],
            },
            created_at="2026-06-01T00:00:02Z",
        )
    )

    view = store.rebuild_session_view(session_id)

    assert view.session_id == session_id
    assert [message.role for message in view.messages] == ["user", "assistant"]
    assert view.messages[0].parts[0].content == "实现 context store"
    assert view.messages[1].parts[0].metadata == {}


def test_jsonl_store_lists_events_in_append_order(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)

    for index in range(3):
        store.append_event(
            SessionEvent(
                id=f"evt_{index}",
                session_id="sess_test",
                type="runtime_state_updated",
                payload={"index": index},
                created_at=f"2026-06-01T00:00:0{index}Z",
            )
        )

    assert [event.id for event in store.list_events("sess_test")] == [
        "evt_0",
        "evt_1",
        "evt_2",
    ]


def test_programmatic_compaction_rebuilds_replaced_parts(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session_id = "sess_test"
    message = AgentMessage(
        id="msg_old",
        session_id=session_id,
        role="user",
        parts=[
            MessagePart(
                id="part_old",
                message_id="msg_old",
                kind="text",
                content="旧任务内容" * 120,
                metadata={"task_hash": "task_old", "created_turn": 1},
            )
        ],
    )
    store.append_event(
        SessionEvent(
            id="evt_user",
            session_id=session_id,
            type="user_message",
            payload={"message_id": message.id, "parts": [message.parts[0].to_dict()]},
        )
    )
    view = SessionView(session_id=session_id, messages=[message])
    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(view=view, active_task_hash="task_current", target_tokens=1, current_turn=10)
    )
    SessionEventWriter(store=store, session_id=session_id).append_compaction_completed(
        trigger="manual",
        target_tokens=1,
        event=result.event,
    )

    rebuilt = store.rebuild_session_view(session_id)

    assert rebuilt.messages[0].parts[0].metadata["compaction_state"] == "micro_compacted"
    assert rebuilt.messages[0].parts[0].content == result.view.messages[0].parts[0].content


def test_l2_archive_placeholder_survives_rebuild_without_l4(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session_id = "sess_test"
    message = AgentMessage(
        id="msg_tool",
        session_id=session_id,
        role="tool",
        parts=[
            MessagePart(
                id="part_tool",
                message_id="msg_tool",
                kind="tool_result",
                content="large tool output\n" * 200,
                metadata={"tool_name": "shell", "tool_call_id": "call_1"},
            )
        ],
    )
    store.append_event(
        SessionEvent(
            id="evt_tool",
            session_id=session_id,
            type="tool_result",
            payload={"message_id": message.id, "parts": [message.parts[0].to_dict()]},
        )
    )
    view = SessionView(session_id=session_id, messages=[message])
    result = CompactionPipeline(root=tmp_path, large_tool_result_tokens=20).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l2",),
        )
    )
    SessionEventWriter(store=store, session_id=session_id).append_compaction_completed(
        trigger="auto",
        target_tokens=1,
        event=result.event,
    )

    rebuilt = store.rebuild_session_view(session_id)
    part = rebuilt.messages[0].parts[0]

    assert part.metadata["compaction_state"] == "archived"
    assert part.metadata["archive_id"]
    assert "archive_id=" in part.content
