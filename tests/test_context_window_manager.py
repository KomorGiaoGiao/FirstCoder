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


class FakePipeline:
    def __init__(self, result: CompactionResult) -> None:
        self.result = result
        self.calls = []

    def compact(self, request):
        self.calls.append(request)
        return self.result


class FakeL4:
    def __init__(self, result: LlmCompactResult) -> None:
        self.result = result
        self.calls = []

    def compact(self, request):
        self.calls.append(request)
        return self.result


class WritingFakeL4:
    def __init__(self, store: JsonlSessionStore) -> None:
        self.store = store

    def compact(self, request):
        checkpoint = Checkpoint(
            id="ckpt_test",
            session_id=request.view.session_id,
            summary="L4 摘要",
            tail_start_message_id="msg_1",
            covered_until_message_id="msg_1",
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


def _l4_result(*, status: str = "success") -> LlmCompactResult:
    return LlmCompactResult(
        checkpoint=None,
        event=LlmCompactEvent(
            status=status,
            source_fingerprint="fp_l4",
            retry_count=0,
            failure_reason=None if status == "success" else "no_summary",
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
    pipeline_result = _programmatic_result(view, before_tokens=1000, after_tokens=100)
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
    assert pipeline.calls[0].target_tokens == 200
    assert [event.type for event in store.list_events("sess_test")] == ["compaction_completed"]


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
