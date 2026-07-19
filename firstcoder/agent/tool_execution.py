"""Tool execution helpers for AgentLoop.

Owns parallel-batch policy, interactive tool sequencing, permission pending
storage, and tool-event emission shape so AgentLoop can stay orchestration-only.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

import anyio

from firstcoder.runtime.cancellation import CancellationToken, cancellation_context
from firstcoder.agent.session import AgentSession, PendingPermissionExecution
from firstcoder.agent.tool_settlement import ToolCallSettlement
from firstcoder.runtime.user_input import UserInputRequest, user_input_request_from_tool_result
from firstcoder.permissions.types import PermissionDecisionKind, PermissionMode, PermissionRequest
from firstcoder.providers.types import ToolCall
from firstcoder.tools.permission_results import make_permission_denied_result, make_prewrite_review_failed_result
from firstcoder.tools.review import build_prewrite_review, supports_prewrite_review
from firstcoder.tools.hidden import HIDDEN_TOOL_STATUS_NAMES
from firstcoder.tools.types import ToolResult, make_error_result

PARALLEL_READONLY_TOOL_NAMES = frozenset(
    {
        "ls",
        "view",
        "grep",
        "glob",
        "tree",
        "read_multi",
        "git_status",
        "git_diff",
        "git_log",
        "diagnostics",
    }
)
BYPASS_PARALLEL_TOOL_NAMES = PARALLEL_READONLY_TOOL_NAMES | frozenset(
    {
        "write",
        "edit",
        "delete",
        "apply_patch",
        "shell",
        "python_exec",
        "fetch",
        "web_search",
    }
)


@dataclass(frozen=True, slots=True)
class ToolExecutionEvent:
    """Runtime-visible tool activity event.

    These events are intentionally separate from provider stream events: provider
    streams describe model output, while this describes local tool execution.
    """

    kind: Literal[
        "prewrite_review",
        "started",
        "finished",
        "permission_requested",
        "denied",
        "skipped",
        "interrupted",
    ]
    tool_call: ToolCall
    result: ToolResult | None = None
    permission_request: PermissionRequest | None = None
    prewrite_review: dict[str, object] | None = None


@dataclass(slots=True)
class ToolExecutionState:
    task_hash_changed: bool = False
    pending_input: UserInputRequest | None = None


@dataclass(slots=True)
class _PermissionPreparation:
    result: ToolResult | None = None
    pending_input: UserInputRequest | None = None
    permission_request: PermissionRequest | None = None


class ToolExecutor:
    """Execute tool batches for one AgentSession."""

    def __init__(
        self,
        *,
        session: AgentSession,
        settlement: ToolCallSettlement,
        emit_event: Callable[..., None],
        check_cancelled: Callable[[], None],
        cancellation_token: CancellationToken | None,
        tag_task_boundary_messages: Callable[[dict[str, object]], None],
        emit_settlements: Callable[[str, object], None],
    ) -> None:
        self.session = session
        self.settlement = settlement
        self._emit_event = emit_event
        self._check_cancelled = check_cancelled
        self.cancellation_token = cancellation_token
        self._tag_task_boundary_messages = tag_task_boundary_messages
        self._emit_settlements = emit_settlements

    def execute_interactive(self, tool_calls: list[ToolCall]) -> ToolExecutionState:
        """执行一个 response 里的全部 tool_calls。

        默认顺序执行。只读探查工具在当前权限允许时可以同批并行，减少等待。
        一旦某个工具返回 pending user input，本轮剩余工具会跳过。
        """

        state = ToolExecutionState()
        index = 0
        while index < len(tool_calls):
            self._check_cancelled()
            tool_call = tool_calls[index]
            if tool_call.name in HIDDEN_TOOL_STATUS_NAMES:
                result = make_error_result(
                    tool_call.name,
                    f"内部控制面工具不可由主模型调用：{tool_call.name}",
                )
                self._emit_event("denied", tool_call, result=result)
                self._record_result(tool_call, result, state=state)
                index += 1
                continue
            permission = self._prepare_permission(tool_call, tool_calls[index + 1 :])
            if permission.result is not None:
                self._emit_event(
                    "denied",
                    tool_call,
                    result=permission.result,
                    permission_request=permission.permission_request,
                )
                self._record_result(tool_call, permission.result, state=state)
                index += 1
                continue
            if permission.pending_input is not None:
                self._emit_event(
                    "permission_requested",
                    tool_call,
                    permission_request=permission.permission_request,
                )
                state.pending_input = permission.pending_input
                return state

            if self.can_execute_in_parallel(tool_call):
                batch_end = self.parallel_readonly_batch_end(tool_calls, index)
                results = self.execute_parallel_readonly_batch(tool_calls[index:batch_end])
                for batch_tool_call, result in zip(tool_calls[index:batch_end], results, strict=True):
                    self._record_result(batch_tool_call, result, state=state)
                index = batch_end
                continue

            result = self.execute_single(tool_call)
            pending_input = self._record_result(
                tool_call,
                result,
                state=state,
                skipped_tool_calls=tool_calls[index + 1 :],
            )
            if pending_input is not None:
                state.pending_input = pending_input
                return state
            index += 1
        return state

    async def execute_interactive_async(self, tool_calls: list[ToolCall]) -> ToolExecutionState:
        """Run the shared synchronous state machine without blocking the stream loop."""

        return await anyio.to_thread.run_sync(self.execute_interactive, tool_calls)

    def _prepare_permission(
        self,
        tool_call: ToolCall,
        skipped_tool_calls: list[ToolCall],
    ) -> _PermissionPreparation:
        """Resolve preflight outcomes before any local tool side effect."""

        preflight = self.session.preflight_tool_call_permission(tool_call)
        if preflight is None:
            return _PermissionPreparation()
        if preflight.decision.kind == PermissionDecisionKind.DENY:
            return _PermissionPreparation(
                result=make_permission_denied_result(
                    tool_name=tool_call.name,
                    request=preflight.request,
                    decision=preflight.decision,
                ),
                permission_request=preflight.request,
            )
        review_only = (
            preflight.decision.kind == PermissionDecisionKind.ALLOW
            and self.requires_prewrite_review(tool_call)
        )
        if preflight.decision.kind == PermissionDecisionKind.ASK or review_only:
            pending = self.store_pending_permission_request(
                tool_call=tool_call,
                request=preflight.request,
                skipped_tool_calls=skipped_tool_calls,
                review_only=review_only,
            )
            if isinstance(pending, ToolResult):
                return _PermissionPreparation(result=pending, permission_request=preflight.request)
            return _PermissionPreparation(pending_input=pending, permission_request=preflight.request)
        return _PermissionPreparation(
            result=self._prepare_bypass_mutation(tool_call, preflight=preflight),
            permission_request=preflight.request,
        )

    def _record_result(
        self,
        tool_call: ToolCall,
        result: ToolResult,
        *,
        state: ToolExecutionState,
        skipped_tool_calls: list[ToolCall] | None = None,
    ) -> UserInputRequest | None:
        self.session.append_tool_result(tool_call=tool_call, result=result)
        pending_input = user_input_request_from_tool_result(
            result,
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
        )
        if pending_input is not None:
            self._emit_settlements("skipped", self.settlement.append_skipped(skipped_tool_calls or []))
            return pending_input
        if tool_call.name == "task_boundary" and result.ok and result.data.get("should_trigger_compaction"):
            self._tag_task_boundary_messages(result.data)
            state.task_hash_changed = True
        return None

    def parallel_readonly_batch_end(self, tool_calls: list[ToolCall], start: int) -> int:
        end = start
        while end < len(tool_calls) and self.can_execute_in_parallel(tool_calls[end]):
            end += 1
        return end

    def can_execute_in_parallel(self, tool_call: ToolCall) -> bool:
        if self.requires_bypass_prewrite_review(tool_call):
            return False
        if tool_call.name not in self.parallel_tool_names_for_current_mode():
            return False
        preflight = self.session.preflight_tool_call_permission(tool_call)
        return preflight is None or preflight.decision.kind == PermissionDecisionKind.ALLOW

    def parallel_tool_names_for_current_mode(self) -> frozenset[str]:
        if self.session.permission_manager is not None and self.session.permission_manager.mode == PermissionMode.BYPASS:
            return BYPASS_PARALLEL_TOOL_NAMES
        return PARALLEL_READONLY_TOOL_NAMES

    def requires_prewrite_review(self, tool_call: ToolCall) -> bool:
        manager = self.session.permission_manager
        return (
            self.session.require_prewrite_review
            and (manager is None or manager.mode != PermissionMode.BYPASS)
            and supports_prewrite_review(tool_call.name)
        )

    def requires_bypass_prewrite_review(self, tool_call: ToolCall) -> bool:
        manager = self.session.permission_manager
        return (
            self.session.require_prewrite_review
            and manager is not None
            and manager.mode == PermissionMode.BYPASS
            and self.session.tool_registry.get(tool_call.name) is not None
            and supports_prewrite_review(tool_call.name)
        )

    def _prepare_bypass_mutation(
        self,
        tool_call: ToolCall,
        *,
        preflight,
    ) -> ToolResult | None:
        if not self.requires_bypass_prewrite_review(tool_call):
            return None
        if preflight is None:
            return None
        review = build_prewrite_review(
            self.session.permission_manager.policy.project_root,
            tool_call,
            access=self.session.sandbox_access,
        )
        if not review.ok:
            return make_prewrite_review_failed_result(
                tool_name=tool_call.name,
                request=preflight.request,
                error=review.error or "未知错误",
            )
        self._emit_event("prewrite_review", tool_call, prewrite_review=review.to_payload())
        return None

    def execute_single(self, tool_call: ToolCall) -> ToolResult:
        self._check_cancelled()
        self._emit_event("started", tool_call)
        with cancellation_context(self.cancellation_token):
            result = self.session.execute_tool_call(tool_call)
        self._emit_event("finished", tool_call, result=result)
        self._check_cancelled()
        return result

    def execute_parallel_readonly_batch(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        self._check_cancelled()
        for tool_call in tool_calls:
            self._emit_event("started", tool_call)
        with ThreadPoolExecutor(max_workers=len(tool_calls)) as executor:
            results = list(executor.map(self.execute_with_cancellation_context, tool_calls))
        for tool_call, result in zip(tool_calls, results, strict=True):
            self._emit_event("finished", tool_call, result=result)
        self._check_cancelled()
        return results

    def execute_with_cancellation_context(self, tool_call: ToolCall) -> ToolResult:
        self._check_cancelled()
        with cancellation_context(self.cancellation_token):
            return self.session.execute_tool_call(tool_call)

    def execute_after_permission_with_cancellation_context(self, tool_call: ToolCall) -> ToolResult:
        self._check_cancelled()
        with cancellation_context(self.cancellation_token):
            return self.session.execute_tool_call_after_permission_confirmation(tool_call)

    def store_pending_permission_request(
        self,
        *,
        tool_call: ToolCall,
        request: PermissionRequest,
        skipped_tool_calls: list[ToolCall],
        review_only: bool = False,
    ) -> UserInputRequest | ToolResult:
        if self.session.permission_manager is None:
            raise RuntimeError("permission confirmation requires a permission manager")

        confirmation = (
            self.session.permission_manager.build_prewrite_review_confirmation(request)
            if review_only
            else self.session.permission_manager.build_confirmation(request)
        )
        prewrite_review = None
        if supports_prewrite_review(tool_call.name):
            prewrite_review = build_prewrite_review(
                self.session.permission_manager.policy.project_root,
                tool_call,
                access=self.session.sandbox_access,
            )
            if not prewrite_review.ok:
                return make_prewrite_review_failed_result(
                    tool_name=tool_call.name,
                    request=request,
                    error=prewrite_review.error or "未知错误",
                )
            confirmation.payload["prewrite_review"] = prewrite_review.to_payload()
        # UI 会看到 confirmation.payload，但恢复时不信任 UI 回传的 tool_call。真实 tool_call
        # 保存在 session.pending_permission_execution 中，避免前端篡改参数后执行。
        trusted_tool_call = ToolCall(
            id=tool_call.id,
            name=tool_call.name,
            arguments=deepcopy(tool_call.arguments),
        )
        confirmation.payload["pending_tool_call"] = {
            "id": trusted_tool_call.id,
            "name": trusted_tool_call.name,
            "arguments": deepcopy(trusted_tool_call.arguments),
        }
        self.session.pending_permission_execution = PendingPermissionExecution(
            request_id=request.id,
            tool_call=trusted_tool_call,
            permission_request=request,
            prewrite_review=prewrite_review,
            review_only=review_only,
            skipped_tool_calls=list(skipped_tool_calls),
        )
        self.session.persist_pending_permission_kind(
            tool_call_id=trusted_tool_call.id,
            review_only=review_only,
        )
        return confirmation

    def permission_input_request_from_pending(self, pending: PendingPermissionExecution) -> UserInputRequest:
        confirmation = self.session.pending_permission_input_request(pending)
        if confirmation is None:
            raise RuntimeError("permission confirmation requires a pending request and permission manager")
        return confirmation
