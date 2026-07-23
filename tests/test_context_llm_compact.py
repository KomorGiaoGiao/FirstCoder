from pathlib import Path

from firstcoder.context.checkpoint import Checkpoint
from firstcoder.context.llm_compact import (
    InvalidLlmCheckpointBoundaryError,
    LlmCompactRequest,
    LlmCompactService,
    LlmCompactSummary,
    LlmSourceFingerprintMismatchError,
    NoSummaryError,
)
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.context.store import JsonlSessionStore


class FakeSummarizer:
    def __init__(self, responses: list[LlmCompactSummary | Exception]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []
        self.summary_modes: list[str] = []

    def summarize(self, messages: list[AgentMessage], *, summary_mode: str = "default") -> LlmCompactSummary:
        self.calls.append([message.id for message in messages])
        self.summary_modes.append(summary_mode)
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


def _request(
    *,
    view: SessionView,
    runtime_state: SessionRuntimeState,
    consumed_tool_result_part_ids: frozenset[str] | None = None,
    **kwargs,
) -> LlmCompactRequest:
    if consumed_tool_result_part_ids is None:
        consumed_tool_result_part_ids = frozenset(
            part.id
            for message in view.messages
            for part in message.parts
            if part.kind == "tool_result"
        )
    return LlmCompactRequest(
        view=view,
        runtime_state=runtime_state,
        consumed_tool_result_part_ids=consumed_tool_result_part_ids,
        **kwargs,
    )


def _generate_and_commit(
    service: LlmCompactService,
    request: LlmCompactRequest,
):
    candidate = service.generate_candidate(request)
    if candidate.event.status == "success":
        service.commit_candidate(candidate, runtime_state=request.runtime_state)
    return candidate


def _tool_transaction_view() -> SessionView:
    call = AgentMessage(
        id="msg_call",
        session_id="sess_test",
        role="assistant",
        parts=[
            MessagePart(
                id="part_call",
                message_id="msg_call",
                kind="tool_call",
                content="",
                metadata={
                    "tool_call_id": "call_1",
                    "tool_name": "shell",
                    "arguments": {"command": "pytest -q"},
                },
            )
        ],
    )
    result = AgentMessage(
        id="msg_tool",
        session_id="sess_test",
        role="tool",
        parts=[
            MessagePart(
                id="part_result",
                message_id="msg_tool",
                kind="tool_result",
                content="3 passed",
                metadata={"tool_call_id": "call_1", "tool_name": "shell", "ok": True},
            )
        ],
    )
    return SessionView(
        session_id="sess_test",
        messages=[call, result, _message("msg_recent", "继续")],
    )


def test_l4_generate_candidate_does_not_write_checkpoint(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    state = SessionRuntimeState(session_id="sess_test")
    service = LlmCompactService(
        store=store,
        summarizer=FakeSummarizer(
            [
                LlmCompactSummary(
                    summary="摘要",
                    tail_start_message_id="msg_2",
                    covered_until_message_id="msg_1",
                )
            ]
        ),
    )
    request = _request(
        view=SessionView(
            session_id="sess_test",
            messages=[_message("msg_1", "旧历史"), _message("msg_2", "tail")],
        ),
        runtime_state=state,
        consumed_tool_result_part_ids=frozenset(),
    )

    candidate = service.generate_candidate(request)

    assert candidate.checkpoint is not None
    assert store.list_events("sess_test") == []
    assert state.latest_checkpoint_id is None

    committed = service.commit_candidate(candidate, runtime_state=state)
    assert committed.id == candidate.checkpoint.id
    assert [event.type for event in store.list_events("sess_test")] == ["checkpoint_created"]


def test_l4_candidate_cannot_cover_unconsumed_tool_transaction(tmp_path) -> None:
    candidate = LlmCompactService(
        store=JsonlSessionStore(tmp_path),
        summarizer=FakeSummarizer(
            [
                LlmCompactSummary(
                    summary="摘要",
                    tail_start_message_id="msg_recent",
                    covered_until_message_id="msg_tool",
                )
            ]
        ),
    ).generate_candidate(
        _request(
            view=_tool_transaction_view(),
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            consumed_tool_result_part_ids=frozenset(),
        )
    )

    assert candidate.checkpoint is None
    assert candidate.event.status == "failed"
    assert candidate.event.failure_reason == "unconsumed_boundary"


def test_l4_candidate_can_cover_consumed_tool_transaction(tmp_path) -> None:
    candidate = LlmCompactService(
        store=JsonlSessionStore(tmp_path),
        summarizer=FakeSummarizer(
            [
                LlmCompactSummary(
                    summary="摘要",
                    tail_start_message_id="msg_recent",
                    covered_until_message_id="msg_tool",
                )
            ]
        ),
    ).generate_candidate(
        _request(
            view=_tool_transaction_view(),
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            consumed_tool_result_part_ids=frozenset({"part_result"}),
        )
    )

    assert candidate.event.status == "success"
    assert candidate.checkpoint is not None
    assert candidate.checkpoint.tail_start_message_id == "msg_recent"


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

    service = LlmCompactService(store=store, summarizer=summarizer)
    result = service.generate_candidate(_request(view=view, runtime_state=state, mode="auto"))
    service.commit_candidate(result, runtime_state=state)

    rebuilt = store.rebuild_session_view("sess_test")
    checkpoint = rebuilt.checkpoints[0]
    assert result.checkpoint.id == checkpoint.id
    assert "## 当前目标\n旧历史摘要" in checkpoint.summary
    assert checkpoint.summary.count("## ") == 7
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

    LlmCompactService(store=JsonlSessionStore(tmp_path), summarizer=summarizer).generate_candidate(_request(view=view, runtime_state=SessionRuntimeState(session_id="sess_test")))

    assert summarizer.calls == [["msg_1", "msg_2"]]


def test_l4_input_uses_latest_checkpoint_summary_plus_tail(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_1", "已由 checkpoint 覆盖"),
            _message("msg_2", "旧 tail"),
            _message("msg_3", "新 tail"),
        ],
        checkpoints=[
            Checkpoint(
                id="ckpt_1",
                session_id="sess_test",
                summary="旧摘要",
                tail_start_message_id="msg_2",
                covered_until_message_id="msg_1",
                source_fingerprint="fp_old",
                sequence=1,
            )
        ],
    )
    summarizer = FakeSummarizer(
        [
            LlmCompactSummary(
                summary="更新摘要",
                tail_start_message_id="msg_3",
                covered_until_message_id="msg_2",
            )
        ]
    )

    result = LlmCompactService(store=store, summarizer=summarizer).generate_candidate(_request(view=view, runtime_state=SessionRuntimeState(session_id="sess_test")))

    assert summarizer.calls == [["ckpt_1_summary", "msg_2", "msg_3"]]
    assert result.checkpoint is not None
    assert result.checkpoint.metadata["source_message_ids"] == ["ckpt_1_summary", "msg_2", "msg_3"]
    assert result.checkpoint.metadata["base_checkpoint_id"] == "ckpt_1"


def test_same_source_fingerprint_is_not_summarized_twice(tmp_path: Path) -> None:
    state = SessionRuntimeState(session_id="sess_test")
    view = SessionView(
        session_id="sess_test",
        messages=[_message("msg_1", "历史"), _message("msg_2", "tail")],
    )
    first_summarizer = FakeSummarizer(
        [
            LlmCompactSummary(
                summary="摘要",
                tail_start_message_id="msg_2",
                covered_until_message_id="msg_1",
            )
        ]
    )
    first_service = LlmCompactService(store=JsonlSessionStore(tmp_path), summarizer=first_summarizer)
    first = first_service.generate_candidate(_request(view=view, runtime_state=state))
    first_service.commit_candidate(first, runtime_state=state)

    second_summarizer = FakeSummarizer([])
    service = LlmCompactService(store=JsonlSessionStore(tmp_path), summarizer=second_summarizer)

    result = service.generate_candidate(
        _request(
            view=view,
            runtime_state=state,
        )
    )

    assert state.last_compaction_input_fingerprint == first.event.source_fingerprint
    assert result.checkpoint is None
    assert result.event.status == "skipped"
    assert result.event.failure_reason == "duplicate_source"
    assert second_summarizer.calls == []


def test_l4_source_fingerprint_includes_latest_checkpoint_boundary(tmp_path: Path) -> None:
    base_messages = [
        _message("msg_1", "已覆盖"),
        _message("msg_2", "tail"),
        _message("msg_3", "new tail"),
    ]
    first_view = SessionView(
        session_id="sess_test",
        messages=base_messages,
        checkpoints=[
            Checkpoint(
                id="ckpt_a",
                session_id="sess_test",
                summary="摘要 A",
                tail_start_message_id="msg_2",
                covered_until_message_id="msg_1",
                source_fingerprint="fp_a",
                sequence=1,
            )
        ],
    )
    second_view = SessionView(
        session_id="sess_test",
        messages=base_messages,
        checkpoints=[
            Checkpoint(
                id="ckpt_b",
                session_id="sess_test",
                summary="摘要 B",
                tail_start_message_id="msg_2",
                covered_until_message_id="msg_1",
                source_fingerprint="fp_b",
                sequence=1,
            )
        ],
    )
    first = LlmCompactService(
        store=JsonlSessionStore(tmp_path / "first"),
        summarizer=FakeSummarizer(
            [
                LlmCompactSummary(
                    summary="更新 A",
                    tail_start_message_id="msg_3",
                    covered_until_message_id="msg_2",
                )
            ]
        ),
    ).generate_candidate(_request(view=first_view, runtime_state=SessionRuntimeState(session_id="sess_test")))
    second = LlmCompactService(
        store=JsonlSessionStore(tmp_path / "second"),
        summarizer=FakeSummarizer(
            [
                LlmCompactSummary(
                    summary="更新 B",
                    tail_start_message_id="msg_3",
                    covered_until_message_id="msg_2",
                )
            ]
        ),
    ).generate_candidate(_request(view=second_view, runtime_state=SessionRuntimeState(session_id="sess_test")))

    assert first.event.source_fingerprint != second.event.source_fingerprint


def test_new_checkpoint_tail_must_move_forward(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_1", "已覆盖"),
            _message("msg_2", "当前 tail"),
            _message("msg_3", "后续"),
        ],
        checkpoints=[
            Checkpoint(
                id="ckpt_1",
                session_id="sess_test",
                summary="旧摘要",
                tail_start_message_id="msg_2",
                covered_until_message_id="msg_1",
                source_fingerprint="fp_old",
                sequence=1,
            )
        ],
    )
    summarizer = FakeSummarizer(
        [
            LlmCompactSummary(
                summary="错误摘要",
                tail_start_message_id="msg_1",
                covered_until_message_id="msg_1",
            )
        ]
    )

    result = LlmCompactService(
        store=JsonlSessionStore(tmp_path),
        summarizer=summarizer,
    ).generate_candidate(_request(view=view, runtime_state=SessionRuntimeState(session_id="sess_test")))

    assert result.checkpoint is None
    assert result.event.status == "failed"
    assert result.event.failure_reason == "invalid_tool_sequence"


def test_new_checkpoint_covered_until_must_be_before_tail_start(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_1", "旧消息"),
            _message("msg_2", "tail 起点"),
            _message("msg_3", "tail 后续"),
        ],
    )
    summarizer = FakeSummarizer(
        [
            LlmCompactSummary(
                summary="错误摘要",
                tail_start_message_id="msg_2",
                covered_until_message_id="msg_3",
            )
        ]
    )

    result = LlmCompactService(
        store=JsonlSessionStore(tmp_path),
        summarizer=summarizer,
    ).generate_candidate(_request(view=view, runtime_state=SessionRuntimeState(session_id="sess_test")))

    assert result.checkpoint is None
    assert result.event.status == "failed"
    assert result.event.failure_reason == "invalid_tool_sequence"


def test_expected_source_fingerprint_mismatch_is_rejected(tmp_path: Path) -> None:
    state = SessionRuntimeState(session_id="sess_test", last_compaction_input_fingerprint="fp_old")
    view = SessionView(
        session_id="sess_test",
        messages=[_message("msg_1", "changed history"), _message("msg_2", "tail")],
    )
    summarizer = FakeSummarizer(
        [
            LlmCompactSummary(
                summary="新摘要",
                tail_start_message_id="msg_2",
                covered_until_message_id="msg_1",
            )
        ]
    )

    try:
        LlmCompactService(store=JsonlSessionStore(tmp_path), summarizer=summarizer).generate_candidate(
            _request(
                view=view,
                runtime_state=state,
                expected_source_fingerprint="fp_old",
            )
        )
    except LlmSourceFingerprintMismatchError as exc:
        assert "expected_source_fingerprint does not match current L4 source" in str(exc)
    else:
        raise AssertionError("expected stale source fingerprint to be rejected")
    assert summarizer.calls == []


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

    service = LlmCompactService(store=JsonlSessionStore(tmp_path), summarizer=summarizer)
    result = service.generate_candidate(
        _request(
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
    service.commit_candidate(result, runtime_state=state)
    assert state.latest_checkpoint_id == result.checkpoint.id


def test_l4_passes_summary_mode_to_summarizer(tmp_path: Path) -> None:
    summarizer = FakeSummarizer(
        [
            LlmCompactSummary(
                summary="更强摘要",
                tail_start_message_id="msg_2",
                covered_until_message_id="msg_1",
            )
        ]
    )

    LlmCompactService(store=JsonlSessionStore(tmp_path), summarizer=summarizer).generate_candidate(
        _request(
            view=SessionView(
                session_id="sess_test",
                messages=[_message("msg_1", "旧历史"), _message("msg_2", "tail")],
            ),
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            summary_mode="stronger",
        )
    )

    assert summarizer.summary_modes == ["stronger"]


def test_l4_candidate_failures_do_not_mutate_circuit_breaker(tmp_path: Path) -> None:
    state = SessionRuntimeState(session_id="sess_test")

    for _ in range(3):
        result = LlmCompactService(
            store=JsonlSessionStore(tmp_path),
            summarizer=FakeSummarizer([NoSummaryError("empty summary"), NoSummaryError("empty summary")]),
        ).generate_candidate(
            _request(
                view=SessionView(
                    session_id="sess_test",
                    messages=[_message("msg_1", "旧历史"), _message("msg_2", "tail")],
                ),
                runtime_state=state,
                mode="auto",
            )
        )

    assert result.checkpoint is None
    assert state.auto_compact_failure_count == 0
    assert state.auto_compact_disabled_until is None
    assert state.last_auto_compact_failure_reason is None


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

    result = LlmCompactService(store=JsonlSessionStore(tmp_path), summarizer=summarizer).generate_candidate(
        _request(
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


def test_l4_rejects_checkpoint_tail_that_starts_with_tool_result(tmp_path: Path) -> None:
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
                        metadata={
                            "tool_call_id": "call_1",
                            "tool_name": "shell",
                            "arguments": {"command": "git status"},
                        },
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
                        content="git status output",
                        metadata={"tool_call_id": "call_1", "tool_name": "shell"},
                    )
                ],
            ),
            _message("msg_tail", "后续用户消息"),
        ],
    )
    summarizer = FakeSummarizer(
        [
            LlmCompactSummary(
                summary="工具调用前半段摘要",
                tail_start_message_id="msg_tool",
                covered_until_message_id="msg_assistant",
            )
        ]
    )

    result = LlmCompactService(
        store=JsonlSessionStore(tmp_path),
        summarizer=summarizer,
    ).generate_candidate(_request(view=view, runtime_state=SessionRuntimeState(session_id="sess_test")))

    assert result.checkpoint is None
    assert result.event.status == "failed"
    assert result.event.failure_reason == "invalid_tool_sequence"
