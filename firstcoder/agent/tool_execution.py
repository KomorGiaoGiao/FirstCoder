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
from firstcoder.agent.background import (
    BackgroundCapacityError,
    BackgroundJob,
    BackgroundJobManager,
    has_background_control_fields,
    make_background_placeholder_result,
    strip_background_controls,
)
from firstcoder.planning.reducer import TaskPlanCommandError, TaskPlanRevisionConflict
from firstcoder.planning.projection import ready_task_ids
from firstcoder.planning.service import TaskPlanService
from firstcoder.runtime.user_input import UserInputRequest, user_input_request_from_tool_result
from firstcoder.permissions.types import PermissionDecisionKind, PermissionMode, PermissionRequest
from firstcoder.providers.types import ToolCall
from firstcoder.tools.permission_results import make_permission_denied_result, make_prewrite_review_failed_result
from firstcoder.tools.review import build_prewrite_review, supports_prewrite_review
from firstcoder.tools.hidden import HIDDEN_TOOL_STATUS_NAMES
from firstcoder.tools.types import ToolResult, make_error_result
from firstcoder.tools.delegate import role_allows_background, role_requires_worktree
from firstcoder.agent.worktree import WorktreeManager

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
        "background_started",
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
        background_manager: BackgroundJobManager | None = None,
        background_tool_names: frozenset[str] | None = None,
    ) -> None:
        self.session = session
        self.settlement = settlement
        self._emit_event = emit_event
        self._check_cancelled = check_cancelled
        self.cancellation_token = cancellation_token
        self._tag_task_boundary_messages = tag_task_boundary_messages
        self._emit_settlements = emit_settlements
        self._background_manager = background_manager
        self._background_tool_names = background_tool_names
        self._background_request: dict[str, tuple[str | None, str | None]] = {}

    def execute_interactive(self, tool_calls: list[ToolCall]) -> ToolExecutionState:
        """执行一个 response 里的全部 tool_calls。

        默认顺序执行。只读探查工具在当前权限允许时可以同批并行，减少等待。
        一旦某个工具返回 pending user input，本轮剩余工具会跳过。
        """

        state = ToolExecutionState()
        # 先一次性把控制面字段（run_in_background/background_label）从每个 tool_call 里剥掉，
        # executor 永远看不到它们。没有控制字段时归一化结果与原 tool_call 完全等价，普通
        # 路径行为不变；后台请求信息按 tool_call_id 单独记录，供本轮调度使用。
        tool_calls, self._background_request = self._normalize_background_controls(tool_calls)
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

            # 权限已放行（ALLOW 或无预检）。此时才允许把请求转入后台，确保绝不后台执行
            # 需要用户确认的工具。
            if tool_call.id in self._background_request:
                label, task_id = self._background_request[tool_call.id]
                result = self._dispatch_background(
                    tool_call,
                    label=label,
                    task_id=task_id,
                )
                self._record_result(tool_call, result, state=state)
                index += 1
                continue

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

    def _normalize_background_controls(
        self,
        tool_calls: list[ToolCall],
    ) -> tuple[list[ToolCall], dict[str, tuple[str | None, str | None]]]:
        """Strip control-plane fields once and record which calls asked for background.

        Returns cleaned tool calls (executor-visible args only) plus a map from
        tool_call_id to the requested background label.  Calls without control
        fields are returned unchanged, so the ordinary path is unaffected.
        """

        cleaned: list[ToolCall] = []
        requested: dict[str, tuple[str | None, str | None]] = {}
        for tool_call in tool_calls:
            if not has_background_control_fields(tool_call.arguments):
                cleaned.append(tool_call)
                continue
            clean_args, run_in_background, label, task_id = strip_background_controls(tool_call.arguments)
            cleaned.append(ToolCall(id=tool_call.id, name=tool_call.name, arguments=clean_args))
            if run_in_background:
                requested[tool_call.id] = (label, task_id)
        return cleaned, requested

    def _dispatch_background(
        self,
        tool_call: ToolCall,
        *,
        label: str | None,
        task_id: str | None,
    ) -> ToolResult:
        """Enqueue a permission-cleared tool call as a background job.

        Returns the immediate placeholder result that closes the original
        ``tool_call_id``.  On any rejection (disabled runtime, ineligible tool,
        or capacity) it returns a normal error result so the model can react and
        the provider history stays valid.
        """

        if self._background_manager is None:
            return make_error_result(
                tool_call.name,
                "后台执行未启用；请去掉 run_in_background 后重试。",
                background_rejected="disabled",
            )
        observed_revision = self._validate_background_task_id(tool_call.name, task_id)
        if isinstance(observed_revision, ToolResult):
            return observed_revision
        allowed = self._background_tool_names
        if allowed is not None and tool_call.name not in allowed:
            return make_error_result(
                tool_call.name,
                f"工具 {tool_call.name} 不支持后台执行；请去掉 run_in_background 后重试。",
                background_rejected="not_allowed",
            )
        if tool_call.name == "delegate" and not self._delegate_call_allows_background(tool_call):
            return make_error_result(
                tool_call.name,
                "delegate 该角色不支持后台执行；仅 researcher/reviewer/tester/coder 可后台运行。",
                background_rejected="role_not_allowed",
            )
        if tool_call.name == "delegate" and self._delegate_call_requires_worktree(tool_call) and not self._worktree_isolation_available():
            return make_error_result(
                tool_call.name,
                "后台 coder 需要 git worktree 隔离，但当前项目不是 git 仓库；请在 git 仓库内使用，或改用前台 coder。",
                background_rejected="worktree_unavailable",
            )
        # 冻结一份可信 tool_call，后台线程执行时不再受外部影响。已通过权限预检，因此
        # 后台执行走“确认后执行”路径，不会二次触发 ASK。
        trusted_arguments = deepcopy(tool_call.arguments)
        # 可变更文件的后台角色（coder）必须在隔离 git worktree 内执行，绝不触碰父工作区。
        # 这里注入内部控制字段 isolate_worktree，delegate executor 会据此走隔离路径。
        if tool_call.name == "delegate" and self._delegate_call_requires_worktree(tool_call):
            if isinstance(trusted_arguments, dict):
                trusted_arguments = {**trusted_arguments, "isolate_worktree": True}
        trusted = ToolCall(id=tool_call.id, name=tool_call.name, arguments=trusted_arguments)

        def run() -> ToolResult:
            return self.session.execute_tool_call_after_permission_confirmation(trusted)

        def complete_task_plan(job: BackgroundJob) -> str | None:
            if job.task_id is not None:
                return self._mark_background_task_completed(
                    job.task_id,
                    observed_revision=job.observed_revision,
                )
            return None

        try:
            job = self._background_manager.start(
                run,
                session_id=self.session.session_id,
                tool_name=tool_call.name,
                label=label,
                task_id=task_id,
                observed_revision=observed_revision,
                on_completed=complete_task_plan if task_id is not None else None,
            )
        except BackgroundCapacityError as exc:
            return make_error_result(
                tool_call.name,
                str(exc),
                background_rejected="capacity",
            )
        self._emit_event("background_started", tool_call)
        return make_background_placeholder_result(job)

    def _task_plan_service(self) -> TaskPlanService:
        return TaskPlanService(store=self.session.store, writer=self.session.writer)

    def _validate_background_task_id(self, tool_name: str, task_id: str | None) -> int | ToolResult | None:
        if task_id is None:
            return None
        plan = self._task_plan_service().current()
        if plan is None:
            return make_error_result(
                tool_name,
                f"Cannot start background work for task_id {task_id!r}: no current task plan. " "Call task_create first, or remove task_id.",
                background_rejected="task_plan_missing",
                task_id=task_id,
            )
        if not any(task.id == task_id for task in plan.tasks):
            return make_error_result(
                tool_name,
                f"Cannot start background work: task_id {task_id!r} is not in the current task plan. " "Call task_list, then retry with an existing task ID.",
                background_rejected="task_not_found",
                task_id=task_id,
                actual_revision=plan.revision,
            )
        return plan.revision

    def _mark_background_task_completed(self, task_id: str, *, observed_revision: int | None) -> str:
        """Advance the still-active task without overwriting newer agent decisions."""

        service = self._task_plan_service()
        for _ in range(3):
            plan = service.current()
            if plan is None:
                return f"TaskPlan task {task_id!r} was not updated because no plan is current."
            task = next((candidate for candidate in plan.tasks if candidate.id == task_id), None)
            if task is None:
                return f"TaskPlan task {task_id!r} was not updated because it no longer exists."
            if task.status in {"completed", "cancelled"}:
                return f"TaskPlan task {task_id!r} was not updated because it is already {task.status}."
            try:
                if task.status == "pending":
                    if plan.revision != observed_revision:
                        return f"TaskPlan task {task_id!r} was not updated because it returned to pending " "after this background job started."
                    if task_id not in ready_task_ids(plan):
                        return f"TaskPlan task {task_id!r} was not updated because it is pending and blocked."
                    plan = service.update(
                        expected_revision=plan.revision,
                        updates=[{"id": task_id, "status": "in_progress"}],
                    ).plan
                service.update(
                    expected_revision=plan.revision,
                    updates=[{"id": task_id, "status": "completed"}],
                )
            except TaskPlanRevisionConflict:
                continue
            except TaskPlanCommandError:
                return f"TaskPlan task {task_id!r} was not updated because its latest state rejects completion."
            return f"TaskPlan task {task_id!r} completed."
        return f"TaskPlan task {task_id!r} was not updated because the plan changed concurrently."

    def _delegate_call_allows_background(self, tool_call: ToolCall) -> bool:
        arguments = tool_call.arguments
        if not isinstance(arguments, dict):
            return False
        return role_allows_background(str(arguments.get("role") or ""))

    def _delegate_call_requires_worktree(self, tool_call: ToolCall) -> bool:
        arguments = tool_call.arguments
        if not isinstance(arguments, dict):
            return False
        return role_requires_worktree(str(arguments.get("role") or ""))

    def _worktree_isolation_available(self) -> bool:
        manager = self.session.permission_manager
        if manager is None:
            return False
        return WorktreeManager(manager.policy.project_root).available()

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
        review_only = preflight.decision.kind == PermissionDecisionKind.ALLOW and self.requires_prewrite_review(tool_call)
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
        if tool_call.id in self._background_request:
            # 请求后台化的调用不能被并行只读批次吞掉；它要走单独的后台调度分支。
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
        return self.session.require_prewrite_review and (manager is None or manager.mode != PermissionMode.BYPASS) and supports_prewrite_review(tool_call.name)

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

        confirmation = self.session.permission_manager.build_prewrite_review_confirmation(request) if review_only else self.session.permission_manager.build_confirmation(request)
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
