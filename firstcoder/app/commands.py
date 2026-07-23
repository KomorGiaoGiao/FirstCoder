"""TUI slash command 处理。

这一层只把用户输入映射到上下文层的结构化能力：`ContextInspector` 用于只读状态，
`ContextWindowManager` 用于手动 compact。Textual widget 不直接读取 JSONL 或拼上下文，
避免 UI 和 agent/context 编排耦合。
"""

from __future__ import annotations

from firstcoder.utils.text import display_value

from dataclasses import dataclass
from typing import Any, Protocol

from firstcoder.context.inspector import ContextInspectionReport, ContextInspector
from firstcoder.app.ports import ContextManagerLike
from firstcoder.context.manager import ContextCompactRequest, ContextCompactResult, ContextWindowTrigger
from firstcoder.context.models import SessionView
from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.context.token_budget import ContextBudget


class SessionLike(Protocol):
    session_id: str
    runtime_state: SessionRuntimeState
    current_turn: int

    def rebuild_view(self) -> SessionView: ...


class BudgetProvider(Protocol):
    def __call__(self, view: SessionView) -> ContextBudget: ...


@dataclass(frozen=True, slots=True)
class CommandResult:
    handled: bool
    output: str = ""
    action: dict[str, Any] | None = None


@dataclass(slots=True)
class ContextCommandHandler:
    """处理 `/context`、`/compact status` 和 `/compact`。"""

    session: SessionLike
    budget_provider: BudgetProvider
    context_manager: ContextManagerLike | None = None
    inspector: ContextInspector = ContextInspector()

    def handle(self, text: str) -> CommandResult:
        command = text.strip()
        if not command.startswith("/"):
            return CommandResult(handled=False)

        normalized = " ".join(command.split())
        if normalized == "/context":
            report = self._inspect()
            return CommandResult(handled=True, output=_render_context_report(report))

        if normalized == "/compact status":
            report = self._inspect()
            return CommandResult(handled=True, output=_render_compact_status(report))

        if normalized == "/compact":
            return CommandResult(handled=True, output=self._manual_compact())

        return CommandResult(handled=False)

    def _inspect(self) -> ContextInspectionReport:
        view = self.session.rebuild_view()
        return self.inspector.inspect(
            view,
            self.session.runtime_state,
            budget=self.budget_provider(view),
        )

    def _manual_compact(self) -> str:
        if self.context_manager is None:
            return "Manual compact unavailable: context manager is not configured"

        view = self.session.rebuild_view()
        budget = self.budget_provider(view)
        result = self.context_manager.compact_if_needed(
            ContextCompactRequest(
                view=view,
                runtime_state=self.session.runtime_state,
                budget=budget,
                estimate_budget=self.budget_provider,
                trigger=ContextWindowTrigger.MANUAL,
                mode="manual",
                current_turn=self.session.current_turn,
                target_tokens=_manual_target_tokens(budget),
            )
        )
        if _is_noop_compact(result):
            return f"Manual compact skipped: {result.programmatic_event.reason} " f"({result.before_tokens} -> {result.after_tokens} tokens)"
        return f"Manual compact {result.status}: {result.reason} " f"({result.before_tokens} -> {result.after_tokens} tokens)"


def _render_context_report(report: ContextInspectionReport) -> str:
    lines = [
        f"Session: {report.session_id}",
        f"Model window: {report.context_window} ({report.context_window_source})",
        f"Output reserve: {report.output_reserve}",
        f"Fixed tokens: {report.fixed_tokens}",
        f"History tokens: {report.history_tokens}",
        f"Input tokens: {report.input_tokens}",
        f"High watermark: {report.high_watermark}",
        f"Low watermark: {report.low_watermark}",
        f"Unconsumed tool results: {report.unconsumed_tool_result_count}",
        f"Tail messages: {report.tail_message_count}",
        f"Latest checkpoint: {display_value(report.latest_checkpoint_id)}",
        f"Checkpoint boundary: {report.checkpoint_boundary_status}",
        f"Archives: {report.archive_count}",
        f"Active task hash: {display_value(report.active_task_hash)}",
        f"Candidate task hash: {display_value(report.candidate_task_hash)}",
        f"System prompt fingerprint: {display_value(report.system_prompt_fingerprint)}",
    ]
    return "\n".join(lines)


def _render_compact_status(report: ContextInspectionReport) -> str:
    lines = [
        f"Auto compact: {report.auto_compact_status}",
        f"Disabled until: {display_value(report.auto_compact_disabled_until)}",
        f"Last failure: {display_value(report.last_failure_reason)}",
        f"Last input fingerprint: {display_value(report.last_compaction_input_fingerprint)}",
        f"Input tokens: {report.input_tokens}",
        f"Tail messages: {report.tail_message_count}",
        f"Latest checkpoint: {display_value(report.latest_checkpoint_id)}",
        "Recent compactions:",
    ]
    if not report.recent_compaction_events:
        lines.append("- none")
    else:
        for event in report.recent_compaction_events:
            lines.append("- " f"{event.get('event_type')} " f"{event.get('trigger')} " f"{event.get('status')} " f"{display_value(event.get('reason'))}")
    return "\n".join(lines)


def _manual_target_tokens(budget: ContextBudget) -> int | None:
    if budget.input_tokens <= 2_000:
        return None
    proposed = min(budget.low_watermark - 1, int(budget.input_tokens * 0.6))
    target = max(budget.fixed_tokens + 1, proposed)
    return target if target < budget.input_tokens else None


def _is_noop_compact(result: ContextCompactResult) -> bool:
    return result.programmatic_event is not None and result.programmatic_event.noop and result.l4_event is None and result.before_tokens == result.after_tokens
