from __future__ import annotations

from firstcoder.context.compaction import CompactionEvent
from firstcoder.app.commands import ContextCommandHandler
from firstcoder.context.manager import ContextCompactResult
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.runtime_state import CompactionHistoryEntry, SessionRuntimeState


class FakeSession:
    def __init__(self) -> None:
        self.session_id = "sess_test"
        self.runtime_state = SessionRuntimeState(session_id="sess_test")
        self.current_turn = 7
        self.view = SessionView(
            session_id="sess_test",
            messages=[
                AgentMessage(
                    id="msg_1",
                    session_id="sess_test",
                    role="user",
                    parts=[
                        MessagePart(
                            id="part_1",
                            message_id="msg_1",
                            kind="text",
                            content="用户消息",
                        )
                    ],
                )
            ],
        )

    def rebuild_view(self) -> SessionView:
        return self.view


class FakeContextManager:
    def __init__(self, result: ContextCompactResult) -> None:
        self.result = result
        self.calls = []

    def compact_if_needed(self, request):
        self.calls.append(request)
        return self.result


def test_context_command_renders_inspection_report() -> None:
    session = FakeSession()
    handler = ContextCommandHandler(session=session)

    result = handler.handle("/context")

    assert result.handled is True
    assert "Session: sess_test" in result.output
    assert "Estimated tokens:" in result.output
    assert "Tail messages: 1" in result.output


def test_compact_status_command_renders_recent_compaction_events() -> None:
    session = FakeSession()
    session.runtime_state.record_compaction_event(
        CompactionHistoryEntry(
            event_type="llm_compaction_completed",
            trigger="auto",
            target_tokens=200,
            input_fingerprint="fp_1",
            status="failed",
            reason="timeout",
            before_tokens=900,
            after_tokens=900,
            checkpoint_id=None,
            created_at="2026-06-02T00:00:00Z",
        )
    )
    handler = ContextCommandHandler(session=session)

    result = handler.handle("/compact status")

    assert result.handled is True
    assert "Auto compact: ready" in result.output
    assert "Recent compactions:" in result.output
    assert "llm_compaction_completed auto failed timeout" in result.output


def test_compact_status_shows_auto_circuit_breaker() -> None:
    session = FakeSession()
    session.runtime_state.auto_compact_disabled_until = "2999-06-01T00:30:00Z"
    session.runtime_state.last_auto_compact_failure_reason = "provider_error"
    handler = ContextCommandHandler(session=session)

    result = handler.handle("/compact status")

    assert result.handled is True
    assert "Auto compact: disabled" in result.output
    assert "Disabled until: 2999-06-01T00:30:00Z" in result.output
    assert "Last failure: provider_error" in result.output


def test_manual_compact_command_calls_context_window_manager() -> None:
    session = FakeSession()
    compact_result = ContextCompactResult(
        status="success",
        reason="manual",
        view=session.view,
        before_tokens=1000,
        after_tokens=200,
    )
    context_manager = FakeContextManager(compact_result)
    handler = ContextCommandHandler(session=session, context_manager=context_manager)

    result = handler.handle("/compact")

    assert result.handled is True
    assert result.output == "Manual compact success: manual (1000 -> 200 tokens)"
    assert len(context_manager.calls) == 1
    assert context_manager.calls[0].trigger == "manual"
    assert context_manager.calls[0].mode == "manual"
    assert context_manager.calls[0].current_turn == 7


def test_manual_compact_uses_lower_target_than_current_context() -> None:
    session = FakeSession()
    session.view.messages[0].parts[0].content = "token " * 20_000
    compact_result = ContextCompactResult(
        status="success",
        reason="manual",
        view=session.view,
        before_tokens=20_000,
        after_tokens=10_000,
    )
    context_manager = FakeContextManager(compact_result)
    handler = ContextCommandHandler(session=session, context_manager=context_manager)

    result = handler.handle("/compact")

    assert result.handled is True
    assert context_manager.calls[0].target_tokens == 12_000


def test_manual_compact_reports_noop_as_skipped() -> None:
    session = FakeSession()
    noop_event = CompactionEvent(
        input_fingerprint="fp_noop",
        before_tokens=1000,
        after_tokens=1000,
        levels_attempted=[],
        stopped_at="already_within_budget",
        changed_parts=0,
        reason="already_within_budget",
        noop=True,
    )
    compact_result = ContextCompactResult(
        status="success",
        reason="manual",
        view=session.view,
        before_tokens=1000,
        after_tokens=1000,
        programmatic_event=noop_event,
    )
    context_manager = FakeContextManager(compact_result)
    handler = ContextCommandHandler(session=session, context_manager=context_manager)

    result = handler.handle("/compact")

    assert result.handled is True
    assert result.output == "Manual compact skipped: already_within_budget (1000 -> 1000 tokens)"


def test_unknown_slash_command_is_reported() -> None:
    handler = ContextCommandHandler(session=FakeSession())

    result = handler.handle("/unknown")

    assert result.handled is False
    assert result.output == ""


def test_plain_input_is_not_handled_as_command() -> None:
    handler = ContextCommandHandler(session=FakeSession())

    result = handler.handle("hello")

    assert result.handled is False
    assert result.output == ""
