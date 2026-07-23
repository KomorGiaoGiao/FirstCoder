"""上下文调试视图。

这一层给 TUI 的 `/context`、`/compact status` 之类入口提供结构化数据。它只读取
`SessionView` 和 `SessionRuntimeState`，不从自然语言消息里反向解析状态，也不触发压缩。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from firstcoder.context.checkpoint import Checkpoint, CheckpointIndex
from firstcoder.context.models import AgentMessage, SessionView
from firstcoder.context.runtime_state import SessionRuntimeState, active_auto_compact_disabled_until
from firstcoder.context.token_budget import ContextBudget

CheckpointBoundaryStatus = Literal["none", "ok", "missing_tail"]
AutoCompactStatus = Literal["ready", "failed", "disabled"]


@dataclass(slots=True)
class ContextInspectionReport:
    session_id: str
    active_task_hash: str | None
    candidate_task_hash: str | None
    system_prompt_fingerprint: str | None
    latest_checkpoint_id: str | None
    tail_message_count: int
    context_window: int
    context_window_source: str
    output_reserve: int
    fixed_tokens: int
    history_tokens: int
    input_tokens: int
    high_watermark: int
    low_watermark: int
    unconsumed_tool_result_count: int
    archive_count: int
    last_compaction_input_fingerprint: str | None
    auto_compact_disabled_until: str | None
    last_failure_reason: str | None
    auto_compact_status: AutoCompactStatus
    checkpoint_boundary_status: CheckpointBoundaryStatus
    recent_compaction_events: list[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TailInspection:
    messages: list[AgentMessage]
    status: CheckpointBoundaryStatus


class ContextInspector:
    """生成上下文状态报告，供调试视图和手动 compact 入口复用。"""

    def inspect(
        self,
        view: SessionView,
        runtime: SessionRuntimeState,
        *,
        budget: ContextBudget,
    ) -> ContextInspectionReport:
        checkpoint = CheckpointIndex(view.checkpoints).latest()
        tail = _inspect_tail_messages(view.messages, checkpoint)
        auto_compact_disabled_until = active_auto_compact_disabled_until(runtime)

        return ContextInspectionReport(
            session_id=view.session_id,
            active_task_hash=runtime.active_task_hash,
            candidate_task_hash=runtime.candidate_task_hash,
            system_prompt_fingerprint=runtime.system_prompt_fingerprint,
            latest_checkpoint_id=checkpoint.id if checkpoint else runtime.latest_checkpoint_id,
            tail_message_count=len(tail.messages),
            context_window=budget.context_window,
            context_window_source=budget.source,
            output_reserve=budget.output_reserve,
            fixed_tokens=budget.fixed_tokens,
            history_tokens=budget.history_tokens,
            input_tokens=budget.input_tokens,
            high_watermark=budget.high_watermark,
            low_watermark=budget.low_watermark,
            unconsumed_tool_result_count=_count_unconsumed_tool_results(
                tail.messages,
                consumed=runtime.consumed_tool_result_part_ids,
            ),
            archive_count=_count_archived_parts(view.messages),
            last_compaction_input_fingerprint=runtime.last_compaction_input_fingerprint,
            auto_compact_disabled_until=auto_compact_disabled_until,
            last_failure_reason=runtime.last_auto_compact_failure_reason,
            auto_compact_status=_auto_compact_status(
                runtime,
                active_disabled_until=auto_compact_disabled_until,
            ),
            checkpoint_boundary_status=tail.status,
            recent_compaction_events=[asdict(event) for event in runtime.recent_compaction_events],
        )


def _inspect_tail_messages(messages: list[AgentMessage], checkpoint: Checkpoint | None) -> TailInspection:
    if checkpoint is None:
        return TailInspection(messages=list(messages), status="none")

    for index, message in enumerate(messages):
        if message.id == checkpoint.tail_start_message_id:
            return TailInspection(messages=list(messages[index:]), status="ok")
    return TailInspection(messages=[], status="missing_tail")


def _count_unconsumed_tool_results(
    messages: list[AgentMessage],
    *,
    consumed: set[str],
) -> int:
    return sum(
        1
        for message in messages
        if message.role == "tool"
        for part in message.parts
        if part.kind == "tool_result" and part.id not in consumed
    )


def _count_archived_parts(messages: list[AgentMessage]) -> int:
    count = 0
    for message in messages:
        for part in message.parts:
            if part.metadata.get("compaction_state") == "archived" or part.metadata.get("archive_id"):
                count += 1
    return count


def _auto_compact_status(
    runtime: SessionRuntimeState,
    *,
    active_disabled_until: str | None,
) -> AutoCompactStatus:
    if active_disabled_until:
        return "disabled"
    if runtime.last_auto_compact_failure_reason:
        return "failed"
    return "ready"
