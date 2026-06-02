from pathlib import Path

from firstcoder.context.llm_compact import (
    LlmCompactRequest,
    LlmCompactService,
    LlmCompactSummary,
    NoSummaryError,
)
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.context.store import JsonlSessionStore


class FakeSummarizer:
    def __init__(self, responses: list[LlmCompactSummary | Exception]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def summarize(self, messages: list[AgentMessage]) -> LlmCompactSummary:
        self.calls.append([message.id for message in messages])
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _message(message_id: str, content: str, *, role: str = "user") -> AgentMessage:
    return AgentMessage(
        id=message_id,
        session_id="sess_test",
        role=role,
        parts=[
            MessagePart(
                id=f"part_{message_id}",
                message_id=message_id,
                kind="text",
                content=content,
            )
        ],
    )


def test_l4_writes_checkpoint_on_success(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_1", "旧历史 1"),
            _message("msg_2", "旧历史 2"),
            _message("msg_3", "最近消息"),
        ],
    )
    state = SessionRuntimeState(session_id="sess_test")
    summarizer = FakeSummarizer(
        [
            LlmCompactSummary(
                summary="旧历史摘要",
                tail_start_message_id="msg_3",
                covered_until_message_id="msg_2",
            )
        ]
    )

    result = LlmCompactService(store=store, summarizer=summarizer).compact(
        LlmCompactRequest(view=view, runtime_state=state, mode="auto")
    )

    rebuilt = store.rebuild_session_view("sess_test")
    checkpoint = rebuilt.checkpoints[0]
    assert result.checkpoint.id == checkpoint.id
    assert checkpoint.summary == "旧历史摘要"
    assert checkpoint.tail_start_message_id == "msg_3"
    assert checkpoint.covered_until_message_id == "msg_2"
    assert checkpoint.source_fingerprint == result.event.source_fingerprint
    assert checkpoint.metadata["summary_prompt_scope"] == "conversation_history_only"
    assert state.latest_checkpoint_id == checkpoint.id
    assert state.auto_compact_failure_count == 0


def test_l4_summary_prompt_scope_excludes_system_prompt_and_tool_schema(tmp_path: Path) -> None:
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
                        kind="text",
                        content="SYSTEM PROMPT AND TOOL SCHEMA",
                    )
                ],
            ),
            _message("msg_1", "用户历史"),
            _message("msg_2", "当前 tail"),
        ],
    )
    summarizer = FakeSummarizer(
        [
            LlmCompactSummary(
                summary="用户历史摘要",
                tail_start_message_id="msg_2",
                covered_until_message_id="msg_1",
            )
        ]
    )

    LlmCompactService(store=JsonlSessionStore(tmp_path), summarizer=summarizer).compact(
        LlmCompactRequest(view=view, runtime_state=SessionRuntimeState(session_id="sess_test"))
    )

    assert summarizer.calls == [["msg_1", "msg_2"]]


def test_l4_retries_no_summary_once_then_succeeds(tmp_path: Path) -> None:
    state = SessionRuntimeState(session_id="sess_test")
    summarizer = FakeSummarizer(
        [
            NoSummaryError("empty summary"),
            LlmCompactSummary(
                summary="重试后的摘要",
                tail_start_message_id="msg_2",
                covered_until_message_id="msg_1",
            ),
        ]
    )

    result = LlmCompactService(store=JsonlSessionStore(tmp_path), summarizer=summarizer).compact(
        LlmCompactRequest(
            view=SessionView(
                session_id="sess_test",
                messages=[_message("msg_1", "旧历史"), _message("msg_2", "tail")],
            ),
            runtime_state=state,
            mode="auto",
        )
    )

    assert result.event.retry_count == 1
    assert len(summarizer.calls) == 2
    assert state.latest_checkpoint_id == result.checkpoint.id


def test_auto_compact_failure_opens_circuit_breaker_after_limit(tmp_path: Path) -> None:
    state = SessionRuntimeState(session_id="sess_test")

    for _ in range(3):
        result = LlmCompactService(
            store=JsonlSessionStore(tmp_path),
            summarizer=FakeSummarizer([NoSummaryError("empty summary"), NoSummaryError("empty summary")]),
        ).compact(
            LlmCompactRequest(
                view=SessionView(
                    session_id="sess_test",
                    messages=[_message("msg_1", "旧历史"), _message("msg_2", "tail")],
                ),
                runtime_state=state,
                mode="auto",
            )
        )

    assert result.checkpoint is None
    assert state.auto_compact_failure_count == 3
    assert state.auto_compact_disabled_until is not None
    assert state.last_auto_compact_failure_reason == "no_summary"


def test_manual_compact_ignores_auto_circuit_breaker(tmp_path: Path) -> None:
    state = SessionRuntimeState(
        session_id="sess_test",
        auto_compact_disabled_until="2099-01-01T00:00:00Z",
    )
    summarizer = FakeSummarizer(
        [
            LlmCompactSummary(
                summary="手动摘要",
                tail_start_message_id="msg_2",
                covered_until_message_id="msg_1",
            )
        ]
    )

    result = LlmCompactService(store=JsonlSessionStore(tmp_path), summarizer=summarizer).compact(
        LlmCompactRequest(
            view=SessionView(
                session_id="sess_test",
                messages=[_message("msg_1", "旧历史"), _message("msg_2", "tail")],
            ),
            runtime_state=state,
            mode="manual",
        )
    )

    assert result.checkpoint is not None
    assert summarizer.calls == [["msg_1", "msg_2"]]
