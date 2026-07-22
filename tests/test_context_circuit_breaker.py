from firstcoder.context.llm_compact import LlmCompactRequest, LlmCompactService, LlmCompactSummary
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.context.store import JsonlSessionStore


class ShouldNotBeCalled:
    def summarize(self, messages, *, summary_mode: str = "default"):
        raise AssertionError("summarizer should not be called while auto compact is disabled")


def test_auto_compact_skips_when_circuit_breaker_is_open(tmp_path) -> None:
    state = SessionRuntimeState(
        session_id="sess_test",
        auto_compact_disabled_until="2099-01-01T00:00:00Z",
    )

    result = LlmCompactService(
        store=JsonlSessionStore(tmp_path),
        summarizer=ShouldNotBeCalled(),
    ).compact(
        LlmCompactRequest(
            view=SessionView(session_id="sess_test"),
            runtime_state=state,
            mode="auto",
        )
    )

    assert result.checkpoint is None
    assert result.event.status == "skipped"
    assert result.event.failure_reason == "circuit_open"


class FakeSummarizer:
    def __init__(self) -> None:
        self.called = False

    def summarize(self, messages, *, summary_mode: str = "default"):
        self.called = True
        return LlmCompactSummary(
            summary="过期熔断后的摘要",
            tail_start_message_id="msg_2",
            covered_until_message_id="msg_1",
        )


def test_auto_compact_resumes_when_circuit_breaker_expired(tmp_path) -> None:
    state = SessionRuntimeState(
        session_id="sess_test",
        auto_compact_disabled_until="2000-01-01T00:00:00Z",
    )
    summarizer = FakeSummarizer()

    result = LlmCompactService(
        store=JsonlSessionStore(tmp_path),
        summarizer=summarizer,
    ).compact(
        LlmCompactRequest(
            view=SessionView(
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
                                content="旧历史",
                            )
                        ],
                    ),
                    AgentMessage(
                        id="msg_2",
                        session_id="sess_test",
                        role="user",
                        parts=[
                            MessagePart(
                                id="part_2",
                                message_id="msg_2",
                                kind="text",
                                content="tail",
                            )
                        ],
                    ),
                ],
            ),
            runtime_state=state,
            mode="auto",
        )
    )

    assert result.event.status == "success"
    assert summarizer.called is True
    assert state.auto_compact_disabled_until is None
