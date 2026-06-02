"""L4 LLM compact 的 MVP 实现。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Literal

from firstcoder.context.checkpoint import Checkpoint
from firstcoder.context.events import SessionEvent
from firstcoder.context.identity import new_event_id, stable_json_hash
from firstcoder.context.models import AgentMessage, SessionView
from firstcoder.context.retry_policy import CompactRetryPolicy
from firstcoder.context.runtime_state import SessionRuntimeState, auto_compact_circuit_is_open
from firstcoder.context.store import JsonlSessionStore


CompactMode = Literal["auto", "manual"]


class PromptTooLongError(RuntimeError):
    pass


class CompactTimeoutError(RuntimeError):
    pass


class NoSummaryError(RuntimeError):
    pass


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

    def summarize(self, messages: list[AgentMessage]) -> LlmCompactSummary:
        ...


@dataclass(slots=True)
class LlmCompactRequest:
    view: SessionView
    runtime_state: SessionRuntimeState
    mode: CompactMode = "auto"


@dataclass(frozen=True, slots=True)
class LlmCompactEvent:
    status: Literal["success", "failed", "skipped"]
    source_fingerprint: str
    retry_count: int = 0
    failure_reason: str | None = None
    checkpoint_id: str | None = None


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
        source_messages = _conversation_messages_only(request.view)
        source_fingerprint = _source_fingerprint(request.view.session_id, source_messages)

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
                summary = self.summarizer.summarize(source_messages)
                checkpoint = self._write_checkpoint(
                    request.view,
                    summary=summary,
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


def _conversation_messages_only(view: SessionView) -> list[AgentMessage]:
    """L4 摘要只看会话历史。

    system prompt、工具 schema 和 provider 能力属于 stable prefix/cache 输入，不属于可被 LLM
    总结折叠的历史。如果把它们混入 summary，resume 时容易污染系统提示词保护边界。
    """

    return [message for message in view.messages if message.role != "system_meta"]


def _source_fingerprint(session_id: str, messages: list[AgentMessage]) -> str:
    return stable_json_hash(
        {
            "session_id": session_id,
            "messages": [message.to_dict() for message in messages],
        },
        length=24,
    )


def _failure_reason(error: Exception) -> str:
    if isinstance(error, PromptTooLongError):
        return "prompt_too_long"
    if isinstance(error, CompactTimeoutError):
        return "timeout"
    if isinstance(error, NoSummaryError):
        return "no_summary"
    return "provider_error"
