"""Phase 1 异步工具运行时的聚焦测试。

覆盖点（对应 docs/async-subagents-dag-plan.md Phase 1）：
- 后台占位结果闭合原始 tool_call，且能通过 validate_tool_call_sequence。
- run_in_background / background_label 在进 executor 前被剥离。
- 完成的后台任务产出独立的 <task_notification> 用户消息，而不是第二条同 id 的 tool_result。
- 失败任务产出 failed 通知。
- 不在允许列表的工具拒绝 run_in_background，返回普通错误结果。
- background_status / background_cancel 对 running/completed/missing id 的行为。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import pytest

from firstcoder.agent.background import (
    DEFAULT_BACKGROUND_TOOL_NAMES,
    BackgroundCapacityError,
    BackgroundJobManager,
    has_background_control_fields,
    render_task_notification,
    strip_background_controls,
    with_background_controls,
)
from firstcoder.agent.loop import AgentLoop
from firstcoder.agent.session import AgentSession
from firstcoder.context.context_builder import ContextBuilder
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.tool_sequence import validate_tool_call_sequence
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import (
    ChatRequest,
    ChatResponse,
    ProviderCapabilities,
    ToolCall,
    ToolDefinition,
)
from firstcoder.tools.background import (
    create_background_cancel_tool,
    create_background_status_tool,
)
from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeProvider(ChatProvider):
    responses: list[ChatResponse]
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
            return ChatResponse(
                provider="fake",
                model="fake-model",
                content='{"decision":"uncertain","basis_message_id":"' + _basis(request) + '"}',
            )
        self.requests.append(request)
        return self.responses.pop(0)


def _basis(request: ChatRequest) -> str:
    for message in reversed(request.messages):
        if message.role == "user":
            for token in str(message.content).split():
                if token.startswith("basis_message_id="):
                    return token.split("=", 1)[1].strip("]")
    return "msg_unknown"


def _bg_tool(name: str, *, executor=None) -> Tool:
    def default_executor(text: str = "") -> ToolResult:
        return ToolResult(name=name, ok=True, content=f"{name}:{text}")

    return Tool(
        definition=ToolDefinition(
            name=name,
            description=f"tool {name}",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
        ),
        executor=executor or default_executor,
    )


def _tool_call(call_id: str, name: str, **arguments) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=dict(arguments))


def _assistant_with_tool_call(tool_call: ToolCall) -> ChatResponse:
    return ChatResponse(
        provider="fake",
        model="fake-model",
        content="",
        tool_calls=[tool_call],
        finish_reason="tool_calls",
    )


# ---------------------------------------------------------------------------
# control-plane helpers
# ---------------------------------------------------------------------------


def test_strip_background_controls_removes_meta_fields() -> None:
    clean, run_in_background, label, task_id = strip_background_controls(
        {
            "text": "hi",
            "run_in_background": True,
            "background_label": " audit ",
            "task_id": " inspect ",
        }
    )
    assert clean == {"text": "hi"}
    assert run_in_background is True
    assert label == "audit"
    assert task_id == "inspect"


def test_strip_background_controls_defaults_when_absent() -> None:
    clean, run_in_background, label, task_id = strip_background_controls({"text": "hi"})
    assert clean == {"text": "hi"}
    assert run_in_background is False
    assert label is None
    assert task_id is None


def test_has_background_control_fields() -> None:
    assert has_background_control_fields({"run_in_background": False}) is True
    assert has_background_control_fields({"background_label": "x"}) is True
    assert has_background_control_fields({"task_id": "inspect"}) is True
    assert has_background_control_fields({"text": "x"}) is False
    assert has_background_control_fields("not-a-dict") is False


def test_with_background_controls_adds_optional_schema_only() -> None:
    base = ToolDefinition(
        name="shell",
        description="run",
        parameters={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    )
    augmented = with_background_controls(base)
    props = augmented.parameters["properties"]
    assert "run_in_background" in props and props["run_in_background"]["type"] == "boolean"
    assert "background_label" in props and props["background_label"]["type"] == "string"
    # 原定义不被修改，且新增字段不进 required。
    assert "task_id" in props and props["task_id"]["type"] == "string"
    assert "run_in_background" not in base.parameters["properties"]
    assert augmented.parameters["required"] == ["command"]


# ---------------------------------------------------------------------------
# BackgroundJobManager
# ---------------------------------------------------------------------------


def test_manager_runs_job_and_emits_completed_notification() -> None:
    manager = BackgroundJobManager()
    try:
        job = manager.start(
            lambda: make_text_result("shell", "hello output"),
            tool_name="shell",
            label="probe",
            task_id="inspect",
            observed_revision=3,
        )
        assert job.id == "bg_0001"
        assert manager.wait(timeout=5) is True
        notifications = manager.collect_completed()
        assert len(notifications) == 1
        note = notifications[0]
        assert note.job_id == "bg_0001"
        assert note.status == "completed"
        assert note.task_id == "inspect"
        assert note.observed_revision == 3
        assert note.ok is True
        assert "hello output" in note.summary
        # 收集后队列清空，不重复上报。
        assert manager.collect_completed() == []
    finally:
        manager.shutdown()


def test_manager_marks_failed_when_tool_result_not_ok() -> None:
    manager = BackgroundJobManager()
    try:
        manager.start(lambda: make_error_result("diagnostics", "boom"), tool_name="diagnostics")
        assert manager.wait(timeout=5) is True
        note = manager.collect_completed()[0]
        assert note.status == "failed"
        assert note.ok is False
        assert "boom" in note.summary
    finally:
        manager.shutdown()


def test_manager_marks_failed_when_function_raises() -> None:
    manager = BackgroundJobManager()

    def boom() -> ToolResult:
        raise RuntimeError("kaboom")

    try:
        manager.start(boom, tool_name="shell")
        assert manager.wait(timeout=5) is True
        note = manager.collect_completed()[0]
        assert note.status == "failed"
        assert "kaboom" in note.summary
    finally:
        manager.shutdown()


def test_manager_enforces_capacity() -> None:
    manager = BackgroundJobManager(max_jobs=1, max_workers=2)
    release = threading.Event()
    try:
        manager.start(lambda: (release.wait(5), make_text_result("shell", "done"))[1], tool_name="shell")
        with pytest.raises(BackgroundCapacityError):
            manager.start(lambda: make_text_result("shell", "second"), tool_name="shell")
        release.set()
        assert manager.wait(timeout=5) is True
    finally:
        release.set()
        manager.shutdown()


def test_manager_cancel_before_start_marks_cancelled() -> None:
    manager = BackgroundJobManager(max_jobs=4, max_workers=1)
    gate = threading.Event()
    started = threading.Event()
    try:
        manager.start(
            lambda: (started.set(), gate.wait(5), make_text_result("shell", "first"))[2],
            tool_name="shell",
        )
        assert started.wait(timeout=5) is True
        queued = manager.start(lambda: make_text_result("shell", "second"), tool_name="shell")
        cancelled = manager.cancel(queued.id)
        assert cancelled is not None
        assert cancelled.status == "cancelled"
        notes = {note.job_id: note for note in manager.collect_completed()}
        assert notes[queued.id].status == "cancelled"
    finally:
        gate.set()
        manager.wait(timeout=5)
        manager.shutdown()


# ---------------------------------------------------------------------------
# ToolExecutor dispatch via AgentLoop
# ---------------------------------------------------------------------------


def _loop_with_manager(store, session_id, *, tools, manager, provider=None):
    session = AgentSession.create(store=store, session_id=session_id)
    provider = provider or FakeProvider([ChatResponse(provider="fake", model="fake-model", content="done")])
    return AgentLoop(
        session=session,
        provider=provider,
        tools=tools,
        background_manager=manager,
    )


def _create_task_plan(session: AgentSession, *, task_id: str = "inspect", status: str = "in_progress") -> None:
    result = session.tool_registry.execute(
        "task_create",
        {
            "mode": "linear",
            "expected_revision": 0,
            "tasks": [{"id": task_id, "content": "Inspect the implementation", "status": status}],
        },
    )
    assert result.ok is True


def test_background_placeholder_closes_tool_call_and_passes_sequence(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    try:
        loop = _loop_with_manager(
            store,
            "sess_bg_placeholder",
            tools=[_bg_tool("shell")],
            manager=manager,
        )
        session = loop.session
        session.append_user_message("go [context: basis_message_id=msg_x]")
        call = _tool_call(
            "call_bg",
            "shell",
            text="ok",
            run_in_background=True,
            task_id="inspect",
        )
        _create_task_plan(session)
        session.append_assistant_response(_assistant_with_tool_call(call))

        state = loop.tool_executor.execute_interactive([call])
        assert state.pending_input is None

        view = session.rebuild_view()
        tool_messages = [m for m in view.messages if m.role == "tool"]
        assert len(tool_messages) == 1
        placeholder = tool_messages[0].parts[0]
        assert placeholder.metadata["tool_call_id"] == "call_bg"
        assert placeholder.metadata["data"]["background_job_id"] == "bg_0001"
        assert placeholder.metadata["data"]["notification_pending"] is True
        assert placeholder.metadata["data"]["task_id"] == "inspect"
        assert placeholder.metadata["data"]["observed_revision"] == 1
        # 占位结果不能让 loop 暂停等待用户输入。
        assert placeholder.metadata["data"].get("requires_user_input") is not True

        # provider 历史序列仍然合法：一个 tool_call 对应恰好一个 tool_result。
        validate_tool_call_sequence(view.messages)
        assert manager.wait(timeout=5) is True
    finally:
        manager.shutdown()


def test_run_in_background_stripped_before_executor(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    seen: dict[str, object] = {}

    def record(text: str = "") -> ToolResult:
        seen["text"] = text
        seen["keys"] = "captured"
        return make_text_result("shell", f"ran:{text}")

    try:
        loop = _loop_with_manager(
            store,
            "sess_bg_strip",
            tools=[_bg_tool("shell", executor=record)],
            manager=manager,
        )
        session = loop.session
        session.append_user_message("go [context: basis_message_id=msg_x]")
        call = _tool_call("call_bg", "shell", text="payload", run_in_background=True, background_label="lbl")
        session.append_assistant_response(_assistant_with_tool_call(call))
        loop.tool_executor.execute_interactive([call])
        assert manager.wait(timeout=5) is True
        # executor 只应看到 text，绝不能收到 run_in_background/background_label（否则 TypeError）。
        assert seen == {"text": "payload", "keys": "captured"}
        note = manager.collect_completed()[0]
        assert note.status == "completed"
        assert note.label == "lbl"
    finally:
        manager.shutdown()


def test_disallowed_tool_rejects_run_in_background(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    try:
        # background_tool_names 限定为空集：任何工具都不允许后台化。
        session = AgentSession.create(store=store, session_id="sess_bg_deny", tools=[_bg_tool("custom")])
        loop = AgentLoop(
            session=session,
            provider=FakeProvider([ChatResponse(provider="fake", model="fake-model", content="done")]),
            background_manager=manager,
            background_tool_names=frozenset(),
        )
        session.append_user_message("go [context: basis_message_id=msg_x]")
        call = _tool_call("call_bg", "custom", text="x", run_in_background=True)
        session.append_assistant_response(_assistant_with_tool_call(call))
        loop.tool_executor.execute_interactive([call])

        view = session.rebuild_view()
        placeholder = [m for m in view.messages if m.role == "tool"][0].parts[0]
        assert placeholder.metadata["ok"] is False
        assert placeholder.metadata["data"].get("background_rejected") == "not_allowed"
        # 拒绝后不应产生后台任务。
        assert manager.list() == []
        validate_tool_call_sequence(view.messages)
    finally:
        manager.shutdown()


def test_background_dispatch_disabled_without_manager(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_bg_off", tools=[_bg_tool("shell")])
    loop = AgentLoop(
        session=session,
        provider=FakeProvider([ChatResponse(provider="fake", model="fake-model", content="done")]),
    )
    session.append_user_message("go [context: basis_message_id=msg_x]")
    call = _tool_call("call_bg", "shell", text="x", run_in_background=True)
    session.append_assistant_response(_assistant_with_tool_call(call))
    loop.tool_executor.execute_interactive([call])

    view = session.rebuild_view()
    placeholder = [m for m in view.messages if m.role == "tool"][0].parts[0]
    assert placeholder.metadata["ok"] is False
    assert placeholder.metadata["data"].get("background_rejected") == "disabled"


def test_background_task_id_requires_current_plan_and_existing_task(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    try:
        loop = _loop_with_manager(
            store,
            "sess_bg_task_validation",
            tools=[_bg_tool("shell")],
            manager=manager,
        )
        session = loop.session
        session.append_user_message("go")
        no_plan = _tool_call("call_no_plan", "shell", text="x", run_in_background=True, task_id="inspect")
        session.append_assistant_response(_assistant_with_tool_call(no_plan))
        loop.tool_executor.execute_interactive([no_plan])
        no_plan_result = [message for message in session.rebuild_view().messages if message.role == "tool"][0].parts[0]
        assert no_plan_result.metadata["ok"] is False
        assert no_plan_result.metadata["data"]["background_rejected"] == "task_plan_missing"
        assert "task_create" in no_plan_result.content

        _create_task_plan(session, task_id="inspect")
        missing = _tool_call("call_missing", "shell", text="x", run_in_background=True, task_id="missing")
        session.append_assistant_response(_assistant_with_tool_call(missing))
        loop.tool_executor.execute_interactive([missing])
        missing_result = [message for message in session.rebuild_view().messages if message.role == "tool"][-1].parts[0]
        assert missing_result.metadata["ok"] is False
        assert missing_result.metadata["data"]["background_rejected"] == "task_not_found"
        assert "task_list" in missing_result.content
        assert manager.list() == []
    finally:
        manager.shutdown()


def test_completed_job_projects_as_notification_not_second_tool_result(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    try:
        loop = _loop_with_manager(
            store,
            "sess_bg_notify",
            tools=[_bg_tool("shell")],
            manager=manager,
        )
        session = loop.session
        session.append_user_message("go [context: basis_message_id=msg_x]")
        call = _tool_call(
            "call_bg",
            "shell",
            text="ok",
            run_in_background=True,
            task_id="inspect",
        )
        _create_task_plan(session)
        session.append_assistant_response(_assistant_with_tool_call(call))
        loop.tool_executor.execute_interactive([call])
        assert manager.wait(timeout=5) is True

        # 模拟下一次 provider 请求前的通知收集。
        loop._append_background_notifications()

        view = session.rebuild_view()
        # 仍然只有一条 tool_result（占位），完成结果是独立的 user 通知。
        tool_messages = [m for m in view.messages if m.role == "tool"]
        assert len(tool_messages) == 1
        notification_messages = [m for m in view.messages if m.role == "user" and "<task_notification>" in m.parts[0].content]
        assert len(notification_messages) == 1
        assert "bg_0001" in notification_messages[0].parts[0].content
        assert "<task_id>inspect</task_id>" in notification_messages[0].parts[0].content
        assert "<observed_revision>1</observed_revision>" in notification_messages[0].parts[0].content
        assert notification_messages[0].parts[0].metadata["background_job_id"] == "bg_0001"
        assert notification_messages[0].parts[0].metadata["background_task_id"] == "inspect"
        assert notification_messages[0].parts[0].metadata["background_observed_revision"] == 1

        plan = session.rebuild_view().task_plan
        assert plan is not None
        assert plan.revision == 2
        assert plan.tasks[0].status == "completed"

        # 通知不会重复注入。
        loop._append_background_notifications()
        view_again = session.rebuild_view()
        assert sum(1 for m in view_again.messages if m.role == "user" and "<task_notification>" in m.parts[0].content) == 1

        # 投影给 provider 时序列合法。
        messages = ContextBuilder().build_provider_messages(view_again, system_prefix=[])
        assert any("<task_notification>" in str(m.content) for m in messages if m.role == "user")
    finally:
        manager.shutdown()


def test_failed_background_task_does_not_claim_task_completed(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    try:
        loop = _loop_with_manager(
            store,
            "sess_bg_task_failure",
            tools=[_bg_tool("shell", executor=lambda text="": make_error_result("shell", "boom"))],
            manager=manager,
        )
        session = loop.session
        _create_task_plan(session)
        session.append_user_message("go")
        call = _tool_call("call_bg", "shell", text="x", run_in_background=True, task_id="inspect")
        session.append_assistant_response(_assistant_with_tool_call(call))
        loop.tool_executor.execute_interactive([call])
        assert manager.wait(timeout=5) is True
        loop._append_background_notifications()

        plan = session.rebuild_view().task_plan
        assert plan is not None
        assert plan.revision == 1
        assert plan.tasks[0].status == "in_progress"
    finally:
        manager.shutdown()


def test_pending_background_task_completes_only_when_collected(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    try:
        loop = _loop_with_manager(
            store,
            "sess_bg_pending_completion",
            tools=[_bg_tool("shell")],
            manager=manager,
        )
        session = loop.session
        _create_task_plan(session, status="pending")
        session.append_user_message("go")
        call = _tool_call("call_bg", "shell", text="x", run_in_background=True, task_id="inspect")
        session.append_assistant_response(_assistant_with_tool_call(call))
        loop.tool_executor.execute_interactive([call])
        assert manager.wait(timeout=5) is True

        before_collection = session.rebuild_view().task_plan
        assert before_collection is not None
        assert before_collection.revision == 1
        assert before_collection.tasks[0].status == "pending"
        assert [event.type for event in store.list_events(session.session_id)].count("task_plan_updated") == 1

        loop._append_background_notifications()

        plan = session.rebuild_view().task_plan
        assert plan is not None
        assert plan.revision == 3
        assert plan.tasks[0].status == "completed"
        assert [event.type for event in store.list_events(session.session_id)].count("task_plan_updated") == 3
    finally:
        manager.shutdown()


def test_task_plan_completion_callback_failure_is_not_reported_as_completed(tmp_path, monkeypatch) -> None:
    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    try:
        loop = _loop_with_manager(
            store,
            "sess_bg_completion_failure",
            tools=[_bg_tool("shell")],
            manager=manager,
        )
        session = loop.session
        _create_task_plan(session)
        session.append_user_message("go")
        call = _tool_call("call_bg", "shell", text="x", run_in_background=True, task_id="inspect")
        session.append_assistant_response(_assistant_with_tool_call(call))
        loop.tool_executor.execute_interactive([call])
        assert manager.wait(timeout=5) is True

        def fail_completion(task_id: str, *, observed_revision: int | None) -> str:
            raise RuntimeError("disk offline")

        monkeypatch.setattr(loop.tool_executor, "_mark_background_task_completed", fail_completion)
        loop._append_background_notifications()

        job = manager.get("bg_0001")
        assert job is not None
        assert job.status == "failed"
        plan = session.rebuild_view().task_plan
        assert plan is not None
        assert plan.revision == 1
        assert plan.tasks[0].status == "in_progress"
        notification = [message.parts[0].content for message in session.rebuild_view().messages if message.role == "user" and "<task_notification>" in message.parts[0].content][0]
        assert "<status>failed</status>" in notification
        assert "TaskPlan completion failed" in notification
        assert "<task_plan_completion>TaskPlan task 'inspect' completed.</task_plan_completion>" not in notification
    finally:
        manager.shutdown()


def test_shared_background_manager_isolates_jobs_by_session(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    started = threading.Event()
    release = threading.Event()

    def blocking(text: str = "") -> ToolResult:
        started.set()
        release.wait(5)
        return make_text_result("shell", f"done:{text}")

    try:
        loop_a = _loop_with_manager(
            store,
            "sess_bg_a",
            tools=[_bg_tool("shell", executor=blocking)],
            manager=manager,
        )
        session_a = loop_a.session
        _create_task_plan(session_a)
        session_a.append_user_message("go")
        call = _tool_call("call_a", "shell", text="a", run_in_background=True, task_id="inspect")
        session_a.append_assistant_response(_assistant_with_tool_call(call))
        loop_a.tool_executor.execute_interactive([call])
        assert started.wait(timeout=5) is True

        loop_b = _loop_with_manager(
            store,
            "sess_bg_b",
            tools=[_bg_tool("shell")],
            manager=manager,
        )
        session_b = loop_b.session
        status_b = session_b.tool_registry.execute("background_status", {})
        assert status_b.ok is True
        assert status_b.data["jobs"] == []
        cancelled_from_b = session_b.tool_registry.execute("background_cancel", {"job_id": "bg_0001"})
        assert cancelled_from_b.ok is False

        release.set()
        assert manager.wait(timeout=5) is True
        loop_b._append_background_notifications()
        view_b = session_b.rebuild_view()
        assert view_b.task_plan is None
        assert not any(message.role == "user" and "<task_notification>" in message.parts[0].content for message in view_b.messages)

        before_a_collection = session_a.rebuild_view().task_plan
        assert before_a_collection is not None
        assert before_a_collection.revision == 1
        assert before_a_collection.tasks[0].status == "in_progress"

        loop_a._append_background_notifications()
        view_a = session_a.rebuild_view()
        assert view_a.task_plan is not None
        assert view_a.task_plan.revision == 2
        assert view_a.task_plan.tasks[0].status == "completed"
        assert sum(1 for message in view_a.messages if message.role == "user" and "<task_notification>" in message.parts[0].content) == 1
    finally:
        release.set()
        manager.shutdown()


def test_cancelled_background_task_does_not_complete_plan_when_tool_ignores_cancel(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    started = threading.Event()
    release = threading.Event()

    def ignore_cancel(text: str = "") -> ToolResult:
        started.set()
        release.wait(5)
        return make_text_result("shell", f"done:{text}")

    try:
        loop = _loop_with_manager(
            store,
            "sess_bg_task_cancel",
            tools=[_bg_tool("shell", executor=ignore_cancel)],
            manager=manager,
        )
        session = loop.session
        _create_task_plan(session)
        session.append_user_message("go")
        call = _tool_call("call_bg", "shell", text="x", run_in_background=True, task_id="inspect")
        session.append_assistant_response(_assistant_with_tool_call(call))
        loop.tool_executor.execute_interactive([call])
        assert started.wait(timeout=5) is True

        cancelled = manager.cancel("bg_0001")
        assert cancelled is not None
        assert cancelled.cancel_requested is True
        release.set()
        assert manager.wait(timeout=5) is True

        job = manager.get("bg_0001")
        assert job is not None
        assert job.status == "cancelled"
        plan = session.rebuild_view().task_plan
        assert plan is not None
        assert plan.revision == 1
        assert plan.tasks[0].status == "in_progress"
        assert [event.type for event in store.list_events(session.session_id)].count("task_plan_updated") == 1
    finally:
        release.set()
        manager.shutdown()


def test_background_completion_does_not_overwrite_newer_task_state(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    gate = threading.Event()
    try:
        loop = _loop_with_manager(
            store,
            "sess_bg_task_conflict",
            tools=[_bg_tool("shell", executor=lambda text="": (gate.wait(5), make_text_result("shell", "done"))[1])],
            manager=manager,
        )
        session = loop.session
        _create_task_plan(session)
        session.append_user_message("go")
        call = _tool_call("call_bg", "shell", text="x", run_in_background=True, task_id="inspect")
        session.append_assistant_response(_assistant_with_tool_call(call))
        loop.tool_executor.execute_interactive([call])

        changed_by_main_agent = session.tool_registry.execute(
            "task_update",
            {"expected_revision": 1, "updates": [{"id": "inspect", "status": "pending"}]},
        )
        assert changed_by_main_agent.ok is True

        gate.set()
        assert manager.wait(timeout=5) is True
        loop._append_background_notifications()

        plan = session.rebuild_view().task_plan
        assert plan is not None
        assert plan.revision == 2
        assert plan.tasks[0].status == "pending"
    finally:
        gate.set()
        manager.shutdown()


# ---------------------------------------------------------------------------
# end-to-end：模型在后续 provider 请求里真的看到通知
# ---------------------------------------------------------------------------


@dataclass
class ReleasingProvider(FakeProvider):
    manager: BackgroundJobManager | None = None
    release: threading.Event = field(default_factory=threading.Event)
    _calls: int = 0

    def complete(self, request: ChatRequest) -> ChatResponse:
        if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
            return super().complete(request)
        self._calls += 1
        if self._calls == 2 and self.manager is not None:
            # 第二次业务调用时释放后台任务并等待其完成，
            # 这样第三次调用前的通知收集一定能拿到结果。
            self.release.set()
            self.manager.wait(timeout=5)
        return super().complete(request)


def test_end_to_end_model_receives_task_notification(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    release_holder: dict[str, threading.Event] = {}

    def blocking(text: str = "") -> ToolResult:
        release_holder["event"].wait(5)
        return make_text_result("shell", f"result:{text}")

    provider = ReleasingProvider(
        responses=[
            _assistant_with_tool_call(_tool_call("call_bg", "shell", text="job", run_in_background=True)),
            _assistant_with_tool_call(_tool_call("call_note", "note", text="waiting")),
            ChatResponse(provider="fake", model="fake-model", content="all done"),
        ],
        manager=manager,
    )
    release_holder["event"] = provider.release

    try:
        session = AgentSession.create(store=store, session_id="sess_bg_e2e")
        loop = AgentLoop(
            session=session,
            provider=provider,
            tools=[_bg_tool("shell", executor=blocking), _bg_tool("note")],
            background_manager=manager,
        )
        result = loop.run_user_turn("run a background job")
        assert result.content == "all done"

        # 第三次 provider 请求应当包含后台完成通知。
        third_request = provider.requests[2]
        notification_texts = [str(m.content) for m in third_request.messages if m.role == "user" and "<task_notification>" in str(m.content)]
        assert notification_texts, "model did not receive a task_notification"
        assert "bg_0001" in notification_texts[0]
        assert "result:job" in notification_texts[0]
    finally:
        release_holder["event"].set()
        manager.wait(timeout=5)
        manager.shutdown()


# ---------------------------------------------------------------------------
# control tools
# ---------------------------------------------------------------------------


def test_background_status_tool_lists_and_reads_jobs() -> None:
    manager = BackgroundJobManager()
    try:
        manager.start(lambda: make_text_result("shell", "ok"), tool_name="shell", label="probe")
        assert manager.wait(timeout=5) is True
        status_tool = create_background_status_tool(manager)

        listed = status_tool.executor()
        assert listed.ok is True
        assert "bg_0001" in listed.content
        assert listed.data["jobs"][0]["job_id"] == "bg_0001"

        one = status_tool.executor(job_id="bg_0001")
        assert one.ok is True
        assert one.data["job"]["tool_name"] == "shell"
        assert one.data["job"]["summary"] == "ok"

        missing = status_tool.executor(job_id="bg_9999")
        assert missing.ok is False
    finally:
        manager.shutdown()


def test_background_cancel_tool_handles_missing_and_existing() -> None:
    manager = BackgroundJobManager(max_workers=1)
    gate = threading.Event()
    try:
        cancel_tool = create_background_cancel_tool(manager)
        missing = cancel_tool.executor(job_id="bg_9999")
        assert missing.ok is False

        manager.start(lambda: (gate.wait(5), make_text_result("shell", "x"))[1], tool_name="shell")
        running = manager.start(lambda: make_text_result("shell", "y"), tool_name="shell")
        cancelled = cancel_tool.executor(job_id=running.id)
        assert cancelled.ok is True
        assert cancelled.data["job"]["job_id"] == running.id
    finally:
        gate.set()
        manager.wait(timeout=5)
        manager.shutdown()


def test_render_task_notification_shape() -> None:
    from firstcoder.agent.background import BackgroundNotification

    text = render_task_notification(
        BackgroundNotification(
            job_id="bg_0001",
            tool_name="shell",
            status="completed",
            summary="done",
            ok=True,
            label="probe",
            task_id="inspect",
            observed_revision=3,
        )
    )
    assert text.startswith("<task_notification>")
    assert "<job_id>bg_0001</job_id>" in text
    assert "<kind>tool</kind>" in text
    assert "<label>probe</label>" in text
    assert "<task_id>inspect</task_id>" in text
    assert "<observed_revision>3</observed_revision>" in text
    assert "<status>completed</status>" in text
    assert text.strip().endswith("</task_notification>")


def test_render_task_notification_escapes_xml_content() -> None:
    from firstcoder.agent.background import BackgroundNotification

    text = render_task_notification(
        BackgroundNotification(
            job_id="bg_0001",
            tool_name="shell",
            status="failed",
            summary="bad <xml> & output",
            ok=False,
            label="a < b & c",
        )
    )
    assert "bad &lt;xml&gt; &amp; output" in text
    assert "a &lt; b &amp; c" in text


def test_agent_loop_registers_background_control_tools_for_custom_tool_sets(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    try:
        session = AgentSession.create(store=store, session_id="sess_bg_control_tools", tools=[_bg_tool("shell")])
        loop = AgentLoop(
            session=session,
            provider=FakeProvider([ChatResponse(provider="fake", model="fake-model", content="done")]),
            background_manager=manager,
        )
        names = loop.session.tool_registry.names()
        assert "background_status" in names
        assert "background_cancel" in names
        definitions = {definition.name: definition for definition in loop._provider_tool_definitions()}
        assert "run_in_background" in definitions["shell"].parameters["properties"]
        assert "run_in_background" not in definitions["background_status"].parameters.get("properties", {})
    finally:
        manager.shutdown()


def test_default_background_tool_names_exclude_control_and_mutation() -> None:
    for excluded in ("task_boundary", "ask_user", "write", "edit", "delete", "apply_patch"):
        assert excluded not in DEFAULT_BACKGROUND_TOOL_NAMES
    for included in ("shell", "grep", "view", "diagnostics"):
        assert included in DEFAULT_BACKGROUND_TOOL_NAMES
