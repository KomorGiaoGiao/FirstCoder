from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.task_boundary import TaskBoundaryDecision, TaskBoundaryService
from firstcoder.context.writer import SessionEventWriter
from firstcoder.planning.models import Task, TaskPlan
from firstcoder.providers.types import ChatResponse, ToolCall
from firstcoder.tools.types import ToolResult


def test_writer_appends_user_assistant_tool_messages_with_valid_parts(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")

    user_id = writer.append_user_message("你好")
    assistant_id = writer.append_assistant_response(
        ChatResponse(
            provider="fake",
            model="fake-model",
            content="我要调用工具",
            tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "abc"})],
            finish_reason="tool_calls",
        )
    )
    tool_id = writer.append_tool_result(
        tool_call=ToolCall(id="call_1", name="echo", arguments={"text": "abc"}),
        result=ToolResult(name="echo", ok=True, content="echo:abc", data={"length": 3}),
    )

    view = store.rebuild_session_view("sess_test")

    assert [message.id for message in view.messages] == [user_id, assistant_id, tool_id]
    assert [message.role for message in view.messages] == ["user", "assistant", "tool"]
    assert view.messages[0].parts[0].kind == "text"
    assert view.messages[1].parts[0].kind == "text"
    assert view.messages[1].parts[1].kind == "tool_call"
    assert view.messages[1].parts[1].metadata["tool_call_id"] == "call_1"
    assert view.messages[2].parts[0].kind == "tool_result"
    assert view.messages[2].parts[0].metadata["tool_call_id"] == "call_1"
    assert view.messages[2].parts[0].metadata["data"] == {"length": 3}
    assert view.messages[0].parts[0].metadata["created_turn"] == 1
    assert view.messages[0].parts[0].metadata["turn_id"] == 1
    assert view.messages[1].parts[0].metadata["created_turn"] == 1
    assert view.messages[1].parts[1].metadata["created_turn"] == 1
    assert view.messages[2].parts[0].metadata["created_turn"] == 1


def test_writer_advances_turn_on_user_messages(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")

    writer.append_user_message("第一轮")
    writer.append_user_message("第二轮")

    view = store.rebuild_session_view("sess_test")
    assert view.messages[0].parts[0].metadata["created_turn"] == 1
    assert view.messages[1].parts[0].metadata["created_turn"] == 2
    assert writer.current_turn == 2


def test_writer_can_patch_message_part_metadata(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    message_id = writer.append_user_message("第一任务")
    part_id = store.rebuild_session_view("sess_test").messages[0].parts[0].id

    writer.append_message_part_metadata_updated(message_id=message_id, part_id=part_id, metadata={"task_hash": "task_a"})

    view = store.rebuild_session_view("sess_test")
    assert view.messages[0].parts[0].metadata["task_hash"] == "task_a"
    assert view.messages[0].parts[0].metadata["created_turn"] == 1


def test_writer_appends_session_created_once_when_requested(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")

    writer.append_session_created(title="demo")

    view = store.rebuild_session_view("sess_test")
    assert view.metadata["session_id"] == "sess_test"
    assert view.metadata["title"] == "demo"


def test_writer_appends_session_metadata_update_event(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")

    writer.append_session_metadata_updated(title="renamed")

    event = store.list_events("sess_test")[0]
    assert event.type == "session_metadata_updated"
    assert event.payload == {"title": "renamed"}


def test_writer_applies_a_consistent_event_envelope(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_event")

    writer.append_session_metadata_updated(title="Demo")

    event = store.list_events("sess_event")[0]
    assert event.id
    assert event.session_id == "sess_event"
    assert event.type == "session_metadata_updated"
    assert event.payload == {"title": "Demo"}


def test_writer_appends_task_boundary_observation_event(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    state = SessionRuntimeState(session_id="sess_test", active_task_hash="task_previous")
    service = TaskBoundaryService(required_stable_count=1)
    observation = service.observe(state, decision=TaskBoundaryDecision.NEW, basis_message_id="msg_new")

    writer.append_task_boundary_observation(observation)

    event = store.list_events("sess_test")[0]
    assert event.type == "task_boundary_observed"
    assert event.payload["active_task_hash"] == observation.candidate_hash
    assert event.payload["triggered_compaction"] is True


def test_writer_appends_task_plan_event_and_store_replays_latest_snapshot(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    initial = TaskPlan(
        mode="linear",
        revision=1,
        tasks=(Task(id="read", content="读代码", status="in_progress"),),
    )
    latest = TaskPlan(
        mode="linear",
        revision=2,
        tasks=(Task(id="read", content="读代码", status="completed"),),
    )

    writer.append_task_plan_updated(
        previous_revision=0,
        operation="create",
        changes=[initial.tasks[0].to_dict()],
        snapshot=initial,
    )
    writer.append_task_plan_updated(
        previous_revision=1,
        operation="update",
        changes=({"id": "read", "status": "completed"},),
        snapshot=latest,
    )

    events = store.list_events("sess_test")
    view = store.rebuild_session_view("sess_test")
    assert [event.type for event in events] == ["task_plan_updated", "task_plan_updated"]
    assert events[-1].payload == {
        "previous_revision": 1,
        "revision": 2,
        "operation": "update",
        "changes": [{"id": "read", "status": "completed"}],
        "snapshot": latest.to_dict(),
    }
    assert view.task_plan == latest


def test_task_plan_state_is_isolated_by_session(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    plan_a = TaskPlan(mode="linear", revision=1, tasks=(Task(id="a", content="会话 A"),))
    plan_b = TaskPlan(mode="dag", revision=1, tasks=(Task(id="b", content="会话 B"),))

    SessionEventWriter(store=store, session_id="sess_a").append_task_plan_updated(
        previous_revision=0,
        operation="create",
        changes=[plan_a.tasks[0].to_dict()],
        snapshot=plan_a,
    )
    SessionEventWriter(store=store, session_id="sess_b").append_task_plan_updated(
        previous_revision=0,
        operation="create",
        changes=[plan_b.tasks[0].to_dict()],
        snapshot=plan_b,
    )

    assert store.rebuild_session_view("sess_a").task_plan == plan_a
    assert store.rebuild_session_view("sess_b").task_plan == plan_b
