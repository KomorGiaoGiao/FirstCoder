"""L4 LLM compact 的 MVP 实现。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Literal

from firstcoder.context.checkpoint import Checkpoint, CheckpointIndex, checkpoint_summary_content
from firstcoder.context.events import SessionEvent
from firstcoder.context.identity import new_event_id, stable_json_hash
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.retry_policy import CompactRetryPolicy
from firstcoder.context.runtime_state import SessionRuntimeState, auto_compact_circuit_is_open
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.tool_sequence import InvalidToolCallSequenceError, validate_tool_call_sequence
from firstcoder.context.versions import CHECKPOINT_STRATEGY_VERSION


CompactMode = Literal["auto", "manual"]


class PromptTooLongError(RuntimeError):
    pass


class CompactTimeoutError(RuntimeError):
    pass


class NoSummaryError(RuntimeError):
    pass


class InvalidLlmCheckpointBoundaryError(ValueError):
    """L4 summarizer 返回的 checkpoint 边界会破坏 resume 投影。"""


class LlmSourceFingerprintMismatchError(ValueError):
    """调用方传入的 expected source fingerprint 与当前 view 不一致。"""


@dataclass(frozen=True, slots=True)
class LlmCompactSummary:
    summary: str
    tail_start_message_id: str
    covered_until_message_id: str


class LlmCompactSummarizer(Protocol):
    """摘要生成器协议。

    真实实现后续可以适配任意 provider；当前上下文层只依赖这个窄协议，避免把 OpenAI、
    Anthropic 等外部消息格式提前泄漏进 checkpoint 写入逻辑。
    """

    def summarize(self, messages: list[AgentMessage], *, summary_mode: str = "default") -> LlmCompactSummary:
        ...


@dataclass(slots=True)
class LlmCompactRequest:
    view: SessionView
    runtime_state: SessionRuntimeState
    mode: CompactMode = "auto"
    expected_source_fingerprint: str | None = None
    summary_mode: str = "default"


@dataclass(frozen=True, slots=True)
class LlmCompactEvent:
    status: Literal["success", "failed", "skipped"]
    source_fingerprint: str
    retry_count: int = 0
    failure_reason: str | None = None
    checkpoint_id: str | None = None
    fallback_steps: list[dict[str, object]] | None = None
    final_failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class LlmCompactResult:
    checkpoint: Checkpoint | None
    event: LlmCompactEvent


@dataclass(slots=True)
class LlmCompactService:
    store: JsonlSessionStore
    summarizer: LlmCompactSummarizer
    retry_policy: CompactRetryPolicy = CompactRetryPolicy()
    auto_failure_limit: int = 3

    def compact(self, request: LlmCompactRequest) -> LlmCompactResult:
        source = _build_l4_source(request.view)
        source_messages = source.messages
        source_fingerprint = _source_fingerprint(request.view.session_id, source)
        if request.expected_source_fingerprint and request.expected_source_fingerprint != source_fingerprint:
            raise LlmSourceFingerprintMismatchError(
                "expected_source_fingerprint does not match current L4 source",
            )

        if request.runtime_state.last_compaction_input_fingerprint == source_fingerprint:
            return LlmCompactResult(
                checkpoint=None,
                event=LlmCompactEvent(
                    status="skipped",
                    source_fingerprint=source_fingerprint,
                    failure_reason="duplicate_source",
                ),
            )

        if request.mode == "auto" and auto_compact_circuit_is_open(request.runtime_state):
            return LlmCompactResult(
                checkpoint=None,
                event=LlmCompactEvent(
                    status="skipped",
                    source_fingerprint=source_fingerprint,
                    failure_reason="circuit_open",
                ),
            )

        attempts = 0
        retries = 0
        while True:
            attempts += 1
            try:
                summary = _summarize(
                    self.summarizer,
                    source_messages,
                    summary_mode=request.summary_mode,
                )
                _validate_summary_boundary(summary, source=source)
                checkpoint = self._write_checkpoint(
                    request.view,
                    summary=summary,
                    source=source,
                    source_fingerprint=source_fingerprint,
                    retry_count=retries,
                )
                request.runtime_state.latest_checkpoint_id = checkpoint.id
                request.runtime_state.last_compaction_input_fingerprint = source_fingerprint
                request.runtime_state.record_auto_compact_success()
                return LlmCompactResult(
                    checkpoint=checkpoint,
                    event=LlmCompactEvent(
                        status="success",
                        source_fingerprint=source_fingerprint,
                        retry_count=retries,
                        checkpoint_id=checkpoint.id,
                    ),
                )
            except (PromptTooLongError, CompactTimeoutError, NoSummaryError) as error:
                reason = _failure_reason(error)
                decision = self.retry_policy.decide(reason, attempt=attempts)
                if not decision.should_retry:
                    if request.mode == "auto":
                        request.runtime_state.record_auto_compact_failure(
                            reason,
                            failure_limit=self.auto_failure_limit,
                        )
                    return LlmCompactResult(
                        checkpoint=None,
                        event=LlmCompactEvent(
                            status="failed",
                            source_fingerprint=source_fingerprint,
                            retry_count=retries,
                            failure_reason=reason,
                        ),
                    )
                retries += 1

    def _write_checkpoint(
        self,
        view: SessionView,
        *,
        summary: LlmCompactSummary,
        source: "L4Source",
        source_fingerprint: str,
        retry_count: int,
    ) -> Checkpoint:
        checkpoint = Checkpoint(
            id="",
            session_id=view.session_id,
            summary=summary.summary,
            tail_start_message_id=summary.tail_start_message_id,
            covered_until_message_id=summary.covered_until_message_id,
            source_fingerprint=source_fingerprint,
            metadata={
                "created_by": "l4_llm_compact",
                "summary_prompt_scope": "conversation_history_only",
                "retry_count": retry_count,
                "base_checkpoint_id": source.base_checkpoint_id,
                "source_message_ids": [message.id for message in source.messages],
            },
        )
        self.store.append_event(
            SessionEvent(
                id=new_event_id(),
                session_id=view.session_id,
                type="checkpoint_created",
                payload=checkpoint.to_dict(),
            )
        )
        return checkpoint


@dataclass(frozen=True, slots=True)
class L4Source:
    messages: list[AgentMessage]
    base_checkpoint_id: str | None = None
    tail_message_ids: tuple[str, ...] = ()


def _conversation_messages_only(view: SessionView) -> list[AgentMessage]:
    """L4 摘要只看会话历史。

    system prompt、工具 schema 和 provider 能力属于 stable prefix/cache 输入，不属于可被 LLM
    总结折叠的历史。如果把它们混入 summary，resume 时容易污染系统提示词保护边界。
    """

    return [message for message in view.messages if message.role != "system_meta"]


def _build_l4_source(view: SessionView) -> L4Source:
    messages = _conversation_messages_only(view)
    checkpoint = CheckpointIndex(view.checkpoints).latest()
    if checkpoint is None:
        return L4Source(messages=messages, tail_message_ids=tuple(message.id for message in messages))

    for index, message in enumerate(messages):
        if message.id == checkpoint.tail_start_message_id:
            tail = messages[index:]
            return L4Source(
                messages=[_checkpoint_summary_message(view.session_id, checkpoint), *tail],
                base_checkpoint_id=checkpoint.id,
                tail_message_ids=tuple(message.id for message in tail),
            )
    raise InvalidLlmCheckpointBoundaryError(
        f"latest checkpoint tail_start_message_id not found: {checkpoint.tail_start_message_id}",
    )


def _checkpoint_summary_message(session_id: str, checkpoint: Checkpoint) -> AgentMessage:
    message_id = f"{checkpoint.id}_summary"
    return AgentMessage(
        id=message_id,
        session_id=session_id,
        role="user",
        parts=[
            MessagePart(
                id=f"part_{message_id}",
                message_id=message_id,
                kind="checkpoint_summary",
                content=checkpoint_summary_content(checkpoint),
                metadata={"checkpoint_id": checkpoint.id},
            )
        ],
        created_at=checkpoint.created_at,
        metadata={"checkpoint_id": checkpoint.id, "synthetic": True},
    )


def _validate_summary_boundary(summary: LlmCompactSummary, *, source: L4Source) -> None:
    if source.base_checkpoint_id is None:
        valid_ids = {message.id for message in source.messages}
    else:
        valid_ids = set(source.tail_message_ids)

    if summary.tail_start_message_id not in valid_ids:
        raise InvalidLlmCheckpointBoundaryError(
            "tail_start_message_id must stay within current L4 input tail",
        )
    if summary.covered_until_message_id not in valid_ids:
        raise InvalidLlmCheckpointBoundaryError(
            "covered_until_message_id must stay within current L4 input tail",
        )

    tail_order = {message_id: index for index, message_id in enumerate(source.tail_message_ids)}
    if tail_order[summary.covered_until_message_id] >= tail_order[summary.tail_start_message_id]:
        raise InvalidLlmCheckpointBoundaryError(
            "covered_until_message_id must be before tail_start_message_id",
        )

    tail_messages = _source_tail_messages(source)
    tail_start_index = tail_order[summary.tail_start_message_id]
    try:
        validate_tool_call_sequence(tail_messages[tail_start_index:])
    except InvalidToolCallSequenceError as error:
        raise InvalidLlmCheckpointBoundaryError(
            "checkpoint tail would break assistant tool_call/tool_result sequence",
        ) from error


def _source_tail_messages(source: L4Source) -> list[AgentMessage]:
    if source.base_checkpoint_id is None:
        return source.messages
    tail_ids = set(source.tail_message_ids)
    return [message for message in source.messages if message.id in tail_ids]


def _source_fingerprint(session_id: str, source: L4Source) -> str:
    return stable_json_hash(
        {
            "session_id": session_id,
            "strategy_version": CHECKPOINT_STRATEGY_VERSION,
            "base_checkpoint_id": source.base_checkpoint_id,
            "tail_message_ids": list(source.tail_message_ids),
            "messages": [message.to_dict() for message in source.messages],
        },
        length=24,
    )


def _summarize(
    summarizer: LlmCompactSummarizer,
    messages: list[AgentMessage],
    *,
    summary_mode: str,
) -> LlmCompactSummary:
    """调用 summarizer，并兼容暂未接收 summary_mode 的旧实现。"""

    try:
        return summarizer.summarize(messages, summary_mode=summary_mode)
    except TypeError as error:
        if "summary_mode" not in str(error):
            raise
        return summarizer.summarize(messages)


def _failure_reason(error: Exception) -> str:
    if isinstance(error, PromptTooLongError):
        return "prompt_too_long"
    if isinstance(error, CompactTimeoutError):
        return "timeout"
    if isinstance(error, NoSummaryError):
        return "no_summary"
    return "provider_error"
