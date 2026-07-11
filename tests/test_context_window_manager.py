from __future__ import annotations

from pathlib import Path

from firstcoder.context.compaction import CompactionEvent, CompactionResult
from firstcoder.context.checkpoint import Checkpoint
from firstcoder.context.events import SessionEvent
from firstcoder.context.llm_compact import LlmCompactEvent, LlmCompactResult
from firstcoder.context.manager import (
    ContextCompactMode,
    ContextCompactRequest,
    ContextWindowManager,
    ContextWindowTrigger,
)
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.triggers import ContextCompactionConfig, evaluate_context_triggers


class FakePipeline:
    def __init__(self, result: CompactionResult | list[CompactionResult]) -> None:
        self.results = list(result) if isinstance(result, list) else [result]
        self.calls = []

    def compact(self, request):
        self.calls.append(request)
        if len(self.results) == 1:
            return self.results[0]
        return self.results.pop(0)


class FakeL4:
    def __init__(self, result: LlmCompactResult | list[LlmCompactResult]) -> None:
        self.results = list(result) if isinstance(result, list) else [result]
        self.calls = []

    def compact(self, request):
        self.calls.append(request)
        if len(self.results) == 1:
            return self.results[0]
        return self.results.pop(0)


class WritingFakeL4:
    def __init__(
        self,
        store: JsonlSessionStore,
        *,
        summary: str = "L4 摘要",
        tail_start_message_id: str = "msg_1",
        covered_until_message_id: str = "msg_1",
    ) -> None:
        self.store = store
        self.summary = summary
        self.tail_start_message_id = tail_start_message_id
        self.covered_until_message_id = covered_until_message_id

    def compact(self, request):
        checkpoint = Checkpoint(
            id="ckpt_test",
            session_id=request.view.session_id,
            summary=self.summary,
            tail_start_message_id=self.tail_start_message_id,
            covered_until_message_id=self.covered_until_message_id,
            source_fingerprint="fp_l4",
        )
        self.store.append_event(
            SessionEvent(
                id="evt_l4",
                session_id=request.view.session_id,
                type="checkpoint_created",
                payload=checkpoint.to_dict(),
            )
        )
        return _l4_result()


def _message(message_id: str, content: str) -> AgentMessage:
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


def _view(*messages: AgentMessage) -> SessionView:
    return SessionView(session_id="sess_test", messages=list(messages))


def _programmatic_result(
    view: SessionView,
    *,
    before_tokens: int = 1000,
    after_tokens: int = 300,
    stopped_at: str = "l1",
) -> CompactionResult:
    return CompactionResult(
        view=view,
        event=CompactionEvent(
            input_fingerprint="fp_programmatic",
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            levels_attempted=["l1"],
            stopped_at=stopped_at,
            changed_parts=1,
        ),
    )


def _l4_result(*, status: str = "success", failure_reason: str | None = None) -> LlmCompactResult:
    return LlmCompactResult(
        checkpoint=None,
        event=LlmCompactEvent(
            status=status,
            source_fingerprint="fp_l4",
            retry_count=0,
            failure_reason=failure_reason if status != "success" else None,
            checkpoint_id="ckpt_test" if status == "success" else None,
        ),
    )


def test_manager_skips_compact_when_under_threshold(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "short"))
    pipeline = FakePipeline(_programmatic_result(view))
    l4 = FakeL4(_l4_result())
    manager = ContextWindowManager(
        store=store,
        pipeline=pipeline,
        l4_service=l4,
        auto_compact_threshold=100,
        target_tokens=80,
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            trigger=ContextWindowTrigger.AUTO,
        )
    )

    assert result.status == "skipped"
    assert result.reason == "under_threshold"
    assert pipeline.calls == []
    assert l4.calls == []
    assert store.list_events("sess_test") == []


def test_manager_runs_pipeline_when_task_hash_changed(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "long" * 400))
    pipeline_result = _programmatic_result(_view(_message("msg_1", "short")), before_tokens=1000, after_tokens=100)
    pipeline = FakePipeline(pipeline_result)
    manager = ContextWindowManager(
        store=store,
        pipeline=pipeline,
        l4_service=FakeL4(_l4_result()),
        auto_compact_threshold=10_000,
        target_tokens=200,
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_test", active_task_hash="task_new"),
            trigger=ContextWindowTrigger.TASK_HASH_CHANGED,
        )
    )

    assert result.status == "success"
    assert result.reason == "task_hash_changed"
    assert result.programmatic_event == pipeline_result.event
    assert len(pipeline.calls) == 1
    assert pipeline.calls[0].active_task_hash == "task_new"
    assert pipeline.calls[0].target_tokens == 133
    assert pipeline.calls[0].required_levels == ("l2", "l3")
    assert pipeline.calls[0].l2_result_target_tokens == 800
    assert pipeline.calls[0].force_route_current_text is False
    assert pipeline.calls[0].force_old_task_compaction is True
    assert [event.type for event in store.list_events("sess_test")] == ["compaction_completed"]


def test_task_switch_uses_explicit_lower_target_and_requires_l2_l3_below_budget(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "short"))
    pipeline = FakePipeline(_programmatic_result(view, before_tokens=5, after_tokens=5, stopped_at="l3"))
    manager = ContextWindowManager(
        store=store,
        pipeline=pipeline,
        l4_service=FakeL4(_l4_result()),
        config=ContextCompactionConfig(
            auto_compact_threshold=10_000,
            target_tokens=900,
            task_switch_target_tokens=600,
            l2_result_target_tokens=77,
        ),
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_test", active_task_hash="task_new"),
            trigger=ContextWindowTrigger.TASK_HASH_CHANGED,
        )
    )

    assert result.status == "success"
    assert pipeline.calls[0].target_tokens == 600
    assert pipeline.calls[0].required_levels == ("l2", "l3")
    assert pipeline.calls[0].l2_result_target_tokens == 77
    assert pipeline.calls[0].force_old_task_compaction is True


def test_manual_and_prompt_too_long_enable_forced_route_compaction(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "long" * 400))
    manager = ContextWindowManager(
        store=store,
        pipeline=FakePipeline(
            [
                _programmatic_result(view, before_tokens=1000, after_tokens=100),
                _programmatic_result(view, before_tokens=1000, after_tokens=100),
                _programmatic_result(view, before_tokens=1000, after_tokens=100),
            ]
        ),
        l4_service=FakeL4(_l4_result()),
        auto_compact_threshold=10,
        target_tokens=200,
    )
    state = SessionRuntimeState(session_id="sess_test")

    manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=state,
            trigger=ContextWindowTrigger.AUTO,
        )
    )
    manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=state,
            trigger=ContextWindowTrigger.MANUAL,
            mode=ContextCompactMode.MANUAL,
        )
    )
    manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=state,
            trigger=ContextWindowTrigger.PROMPT_TOO_LONG,
        )
    )

    assert manager.pipeline.calls[0].force_route_current_text is False
    assert manager.pipeline.calls[1].force_route_current_text is True
    assert manager.pipeline.calls[2].force_route_current_text is True


def test_manager_runs_l4_only_after_l1_l3_fail_target(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "long" * 400))
    pipeline = FakePipeline(
        _programmatic_result(
            view,
            before_tokens=1000,
            after_tokens=900,
            stopped_at="not_reached",
        )
    )
    l4 = FakeL4(_l4_result())
    manager = ContextWindowManager(
        store=store,
        pipeline=pipeline,
        l4_service=l4,
        auto_compact_threshold=10,
        target_tokens=200,
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            trigger=ContextWindowTrigger.AUTO,
        )
    )

    assert result.status == "success"
    assert result.l4_event is not None
    assert len(l4.calls) == 1
    assert l4.calls[0].mode == "auto"
    assert [event.type for event in store.list_events("sess_test")] == [
        "compaction_completed",
        "llm_compaction_completed",
    ]


def test_manager_persists_l4_missing_failure_for_replay(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "long" * 400))
    pipeline = FakePipeline(
        _programmatic_result(
            view,
            before_tokens=1000,
            after_tokens=900,
            stopped_at="not_reached",
        )
    )
    manager = ContextWindowManager(
        store=store,
        pipeline=pipeline,
        l4_service=None,
        auto_compact_threshold=10,
        target_tokens=200,
    )
    runtime_state = SessionRuntimeState(session_id="sess_test")

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=runtime_state,
            trigger=ContextWindowTrigger.AUTO,
        )
    )

    events = store.list_events("sess_test")
    assert result.status == "failed"
    assert result.reason == "l4_service_missing"
    assert [event.type for event in events] == ["compaction_completed", "llm_compaction_completed"]
    assert events[-1].payload["status"] == "failed"
    assert events[-1].payload["reason"] == "l4_service_missing"


def test_manager_uses_effective_tokens_after_programmatic_compaction(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_old", "old raw history" * 800),
            AgentMessage(
                id="msg_tail_tool",
                session_id="sess_test",
                role="tool",
                parts=[
                    MessagePart(
                        id="part_tail_tool",
                        message_id="msg_tail_tool",
                        kind="tool_result",
                        content="large tail tool output\n" * 100,
                        metadata={"tool_call_id": "call_1", "tool_name": "shell"},
                    )
                ],
            ),
        ],
        checkpoints=[
            Checkpoint(
                id="ckpt_1",
                session_id="sess_test",
                summary="old summary",
                tail_start_message_id="msg_tail_tool",
                covered_until_message_id="msg_old",
                source_fingerprint="fp_1",
                sequence=1,
            )
        ],
    )
    l4 = FakeL4(_l4_result())
    manager = ContextWindowManager(
        store=store,
        l4_service=l4,
        config=ContextCompactionConfig(
            auto_compact_threshold=10_000,
            target_tokens=1_000,
            large_tool_result_tokens=20,
        ),
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            trigger=ContextWindowTrigger.AUTO,
        )
    )

    assert result.status == "success"
    assert result.reason == "large_tool_result"
    assert result.l4_event is None
    assert l4.calls == []
    assert result.after_tokens <= 1_000


def test_manager_returns_rebuilt_view_after_l4_writes_checkpoint(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "long" * 400))
    store.append_event(
        SessionEvent(
            id="evt_user",
            session_id="sess_test",
            type="user_message",
            payload={
                "message_id": "msg_1",
                "parts": [view.messages[0].parts[0].to_dict()],
            },
        )
    )
    manager = ContextWindowManager(
        store=store,
        pipeline=FakePipeline(_programmatic_result(view, after_tokens=900, stopped_at="not_reached")),
        l4_service=WritingFakeL4(store),
        auto_compact_threshold=10,
        target_tokens=200,
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            trigger=ContextWindowTrigger.AUTO,
        )
    )

    assert result.status == "success"
    assert [checkpoint.id for checkpoint in result.view.checkpoints] == ["ckpt_test"]


def test_manager_reports_effective_tokens_after_l4_rebuild(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(
        _message("msg_old", "old context " * 4_000),
        _message("msg_tail", "short tail"),
    )
    for message in view.messages:
        store.append_event(
            SessionEvent(
                id=f"evt_{message.id}",
                session_id="sess_test",
                type="user_message",
                payload={
                    "message_id": message.id,
                    "parts": [message.parts[0].to_dict()],
                },
            )
        )
    config = ContextCompactionConfig(auto_compact_threshold=10, target_tokens=200)
    manager = ContextWindowManager(
        store=store,
        pipeline=FakePipeline(_programmatic_result(view, after_tokens=5_001, stopped_at="not_reached")),
        l4_service=WritingFakeL4(
            store,
            summary="short checkpoint",
            tail_start_message_id="msg_tail",
            covered_until_message_id="msg_old",
        ),
        config=config,
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            trigger=ContextWindowTrigger.AUTO,
        )
    )

    rebuilt_tokens = evaluate_context_triggers(result.view, config).estimated_tokens
    assert result.status == "success"
    assert result.after_tokens == rebuilt_tokens
    assert result.after_tokens < 5_001


def test_manual_compact_ignores_auto_circuit_breaker(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "long" * 400))
    l4 = FakeL4(_l4_result())
    manager = ContextWindowManager(
        store=store,
        pipeline=FakePipeline(_programmatic_result(view, after_tokens=900, stopped_at="not_reached")),
        l4_service=l4,
        auto_compact_threshold=10_000,
        target_tokens=200,
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(
                session_id="sess_test",
                auto_compact_disabled_until="2099-01-01T00:00:00Z",
            ),
            trigger=ContextWindowTrigger.MANUAL,
            mode=ContextCompactMode.MANUAL,
        )
    )

    assert result.status == "success"
    assert len(l4.calls) == 1
    assert l4.calls[0].mode == "manual"


def test_task_hash_changed_ignores_auto_circuit_breaker(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "old task text" * 80))
    pipeline = FakePipeline(_programmatic_result(view, after_tokens=100, stopped_at="l1"))
    manager = ContextWindowManager(
        store=store,
        pipeline=pipeline,
        l4_service=FakeL4(_l4_result()),
        auto_compact_threshold=10_000,
        target_tokens=200,
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(
                session_id="sess_test",
                active_task_hash="task_new",
                auto_compact_disabled_until="2099-01-01T00:00:00Z",
            ),
            trigger=ContextWindowTrigger.TASK_HASH_CHANGED,
        )
    )

    assert result.status == "success"
    assert len(pipeline.calls) == 1
    assert pipeline.calls[0].force_old_task_compaction is True


def test_manual_compact_honors_explicit_lower_target(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "long" * 4_000))
    pipeline = FakePipeline(_programmatic_result(view, after_tokens=900, stopped_at="not_reached"))
    l4 = FakeL4(_l4_result())
    manager = ContextWindowManager(
        store=store,
        pipeline=pipeline,
        l4_service=l4,
        auto_compact_threshold=10_000,
        target_tokens=2_000,
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            trigger=ContextWindowTrigger.MANUAL,
            mode=ContextCompactMode.MANUAL,
            target_tokens=800,
        )
    )

    assert result.status == "success"
    assert pipeline.calls[0].target_tokens == 800
    assert len(l4.calls) == 1


def test_manager_handles_prompt_too_long_as_blocking_trigger(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "long" * 400))
    pipeline = FakePipeline(_programmatic_result(view, after_tokens=100))
    manager = ContextWindowManager(
        store=store,
        pipeline=pipeline,
        l4_service=FakeL4(_l4_result()),
        auto_compact_threshold=10_000,
        target_tokens=200,
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            trigger=ContextWindowTrigger.PROMPT_TOO_LONG,
        )
    )

    assert result.status == "success"
    assert result.reason == "prompt_too_long"
    assert pipeline.calls[0].target_tokens == 200


def test_manager_runs_stronger_programmatic_fallback_after_prompt_too_long_l4_failure(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "long" * 800))
    first_programmatic = _programmatic_result(view, before_tokens=1200, after_tokens=900, stopped_at="not_reached")
    stronger_view = _view(_message("msg_1", "short"))
    stronger_programmatic = _programmatic_result(
        stronger_view,
        before_tokens=900,
        after_tokens=100,
        stopped_at="l1",
    )
    pipeline = FakePipeline([first_programmatic, stronger_programmatic])
    l4 = FakeL4(_l4_result(status="failed", failure_reason="prompt_too_long"))
    manager = ContextWindowManager(
        store=store,
        pipeline=pipeline,
        l4_service=l4,
        auto_compact_threshold=10,
        target_tokens=200,
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            trigger=ContextWindowTrigger.AUTO,
        )
    )

    assert result.status == "success"
    assert result.after_tokens <= 200
    assert len(pipeline.calls) == 2
    assert len(l4.calls) == 1
    assert result.fallback_steps[0]["action"] == "stronger_programmatic"
    assert result.fallback_steps[0]["status"] == "success"


def test_programmatic_fallback_success_records_successful_l4_event_for_replay(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "long" * 800))
    first_programmatic = _programmatic_result(view, before_tokens=1200, after_tokens=900, stopped_at="not_reached")
    stronger_view = _view(_message("msg_1", "short"))
    stronger_programmatic = _programmatic_result(stronger_view, before_tokens=900, after_tokens=100, stopped_at="l1")
    manager = ContextWindowManager(
        store=store,
        pipeline=FakePipeline([first_programmatic, stronger_programmatic]),
        l4_service=FakeL4(_l4_result(status="failed", failure_reason="prompt_too_long")),
        auto_compact_threshold=10,
        target_tokens=200,
    )

    manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            trigger=ContextWindowTrigger.AUTO,
        )
    )

    l4_events = [event for event in store.list_events("sess_test") if event.type == "llm_compaction_completed"]
    assert l4_events[0].payload["status"] == "success"
    assert l4_events[0].payload["reason"] == "fallback_success"
    assert l4_events[0].payload["event"]["fallback_steps"][0]["status"] == "success"


def test_prompt_too_long_fallback_retries_l4_when_still_over_budget(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "long" * 800))
    first_programmatic = _programmatic_result(view, before_tokens=1200, after_tokens=900, stopped_at="not_reached")
    stronger_programmatic = _programmatic_result(view, before_tokens=900, after_tokens=800, stopped_at="not_reached")
    pipeline = FakePipeline([first_programmatic, stronger_programmatic])
    l4 = FakeL4(
        [
            _l4_result(status="failed", failure_reason="prompt_too_long"),
            _l4_result(status="success"),
        ]
    )
    manager = ContextWindowManager(
        store=store,
        pipeline=pipeline,
        l4_service=l4,
        auto_compact_threshold=10,
        target_tokens=200,
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            trigger=ContextWindowTrigger.AUTO,
        )
    )

    assert result.status == "success"
    assert len(pipeline.calls) == 2
    assert len(l4.calls) == 2
    assert l4.calls[1].summary_mode == "stronger"
    assert result.fallback_steps[0]["action"] == "stronger_programmatic"
    assert result.fallback_steps[0]["status"] == "failed"
    assert result.fallback_steps[1]["action"] == "retry_l4_stronger_summary"
    assert result.fallback_steps[1]["status"] == "success"


def test_prompt_too_long_retry_records_one_l4_event_with_fallback_steps(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "long" * 800))
    pipeline = FakePipeline(
        [
            _programmatic_result(view, before_tokens=1200, after_tokens=900, stopped_at="not_reached"),
            _programmatic_result(view, before_tokens=900, after_tokens=800, stopped_at="not_reached"),
        ]
    )
    l4 = FakeL4(
        [
            _l4_result(status="failed", failure_reason="prompt_too_long"),
            _l4_result(status="success"),
        ]
    )
    manager = ContextWindowManager(
        store=store,
        pipeline=pipeline,
        l4_service=l4,
        auto_compact_threshold=10,
        target_tokens=200,
    )

    manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            trigger=ContextWindowTrigger.AUTO,
        )
    )

    l4_events = [event for event in store.list_events("sess_test") if event.type == "llm_compaction_completed"]
    assert len(l4_events) == 1
    assert l4_events[0].payload["status"] == "success"
    assert len(l4_events[0].payload["event"]["fallback_steps"]) == 2


def test_manager_retries_l4_once_after_no_summary_with_stronger_summary_mode(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "long" * 800))
    pipeline = FakePipeline(_programmatic_result(view, before_tokens=1000, after_tokens=900, stopped_at="not_reached"))
    l4 = FakeL4(
        [
            _l4_result(status="failed", failure_reason="no_summary"),
            _l4_result(status="success"),
        ]
    )
    manager = ContextWindowManager(
        store=store,
        pipeline=pipeline,
        l4_service=l4,
        auto_compact_threshold=10,
        target_tokens=200,
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            trigger=ContextWindowTrigger.AUTO,
        )
    )

    assert result.status == "success"
    assert len(l4.calls) == 2
    assert l4.calls[1].summary_mode == "stronger"
    assert result.fallback_steps[0]["action"] == "retry_l4_stronger_summary"
    assert result.fallback_steps[0]["status"] == "success"


def test_manager_records_fallback_steps_in_l4_event_payload(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "long" * 800))
    pipeline = FakePipeline(_programmatic_result(view, before_tokens=1000, after_tokens=900, stopped_at="not_reached"))
    l4 = FakeL4(
        [
            _l4_result(status="failed", failure_reason="no_summary"),
            _l4_result(status="success"),
        ]
    )
    manager = ContextWindowManager(
        store=store,
        pipeline=pipeline,
        l4_service=l4,
        auto_compact_threshold=10,
        target_tokens=200,
    )

    manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            trigger=ContextWindowTrigger.AUTO,
        )
    )

    events = store.list_events("sess_test")
    l4_events = [event for event in events if event.type == "llm_compaction_completed"]
    assert len(l4_events) == 1
    assert l4_events[0].payload["event"]["fallback_steps"][0]["action"] == "retry_l4_stronger_summary"
    assert l4_events[0].payload["event"]["final_failure_reason"] is None


def test_manual_compact_reports_fallback_failure_reason(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "long" * 800))
    pipeline = FakePipeline(_programmatic_result(view, before_tokens=1000, after_tokens=900, stopped_at="not_reached"))
    l4 = FakeL4(
        [
            _l4_result(status="failed", failure_reason="provider_error"),
        ]
    )
    manager = ContextWindowManager(
        store=store,
        pipeline=pipeline,
        l4_service=l4,
        auto_compact_threshold=10,
        target_tokens=200,
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            trigger=ContextWindowTrigger.MANUAL,
            mode=ContextCompactMode.MANUAL,
        )
    )

    assert result.status == "failed"
    assert result.final_failure_reason == "provider_error"
    assert result.fallback_steps[0]["status"] == "failed"


def test_auto_compact_failure_after_fallback_updates_circuit_breaker(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = _view(_message("msg_1", "long" * 800))
    state = SessionRuntimeState(session_id="sess_test", auto_compact_failure_count=2)
    pipeline = FakePipeline(_programmatic_result(view, before_tokens=1000, after_tokens=900, stopped_at="not_reached"))
    l4 = FakeL4(_l4_result(status="failed", failure_reason="provider_error"))
    manager = ContextWindowManager(
        store=store,
        pipeline=pipeline,
        l4_service=l4,
        auto_compact_threshold=10,
        target_tokens=200,
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=state,
            trigger=ContextWindowTrigger.AUTO,
        )
    )

    assert result.status == "failed"
    assert state.auto_compact_failure_count == 3
    assert state.auto_compact_disabled_until is not None
