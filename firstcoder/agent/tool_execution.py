"""Tool execution helpers for AgentLoop.

Owns parallel-batch policy, interactive tool sequencing, permission pending
storage, and tool-event emission shape so AgentLoop can stay orchestration-only.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal

import anyio

from firstcoder.runtime.cancellation import CancellationToken, cancellation_context
from firstcoder.agent.session import AgentSession, PendingPermissionExecution
from firstcoder.agent.tool_settlement import ToolCallSettlement
from firstcoder.runtime.user_input import UserInputRequest, user_input_request_from_tool_result
from firstcoder.agent.verification import is_successful_verification_result
from firstcoder.permissions.types import PermissionDecisionKind, PermissionMode, PermissionRequest
from firstcoder.providers.types import ToolCall
from firstcoder.tools.permission_results import make_permission_denied_result
from firstcoder.tools.types import ToolResult

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

    kind: Literal["started", "finished", "permission_requested", "denied", "skipped", "interrupted"]
    tool_call: ToolCall
    result: ToolResult | None = None
    permission_request: PermissionRequest | None = None


@dataclass(slots=True)
class ToolExecutionState:
    task_hash_changed: bool = False
    pending_input: UserInputRequest | None = None
    successful_verification: bool = False


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

    def execute(self, tool_calls: list[ToolCall]) -> bool:
        """兼容旧入口：执行工具调用并返回是否发生 task hash 变化。"""

        return self.execute_interactive(tool_calls).task_hash_changed

    def execute_interactive(self, tool_calls: list[ToolCall]) -> ToolExecutionState:
        """执行一个 response 里的全部 tool_calls。

        默认顺序执行。只读探查工具在当前权限允许时可以同批并行，减少等待。
        一旦某个工具返回 pending user input，本轮剩余工具会跳过。
        """

        task_hash_changed = False
        successful_verification = False
        index = 0
        while index < len(tool_calls):
            self._check_cancelled()
            tool_call = tool_calls[index]
            # 权限检查放在工具执行前，但具体“这个路径能不能写 / 这个命令能不能跑”
            # 的判断由 permissions 和 permission-aware tool wrapper 完成。AgentLoop 只关心
            # allow / deny / ask 三种结果该如何写回会话。
            preflight = self.session.preflight_tool_call_permission(tool_call)
            if preflight is not None:
                if preflight.decision.kind == PermissionDecisionKind.DENY:
                    result = make_permission_denied_result(
                        tool_name=tool_call.name,
                        request=preflight.request,
                        decision=preflight.decision,
                    )
                    self._emit_event(
                        "denied",
                        tool_call,
                        result=result,
                        permission_request=preflight.request,
                    )
                    self.session.append_tool_result(tool_call=tool_call, result=result)
                    index += 1
                    continue
                if preflight.decision.kind == PermissionDecisionKind.ASK:
                    # 需要用户确认时不能继续执行同批次后续工具。否则用户还没批准第一个
                    # 高风险操作，后面的工具却已经产生副作用了。
                    pending_input = self.store_pending_permission_request(
                        tool_call=tool_call,
                        request=preflight.request,
                        skipped_tool_calls=tool_calls[index + 1 :],
                    )
                    self._emit_event(
                        "permission_requested",
                        tool_call,
                        permission_request=preflight.request,
                    )
                    return ToolExecutionState(
                        task_hash_changed=task_hash_changed,
                        pending_input=pending_input,
                        successful_verification=successful_verification,
                    )

            if self.can_execute_in_parallel(tool_call):
                batch_end = self.parallel_readonly_batch_end(tool_calls, index)
                results = self.execute_parallel_readonly_batch(tool_calls[index:batch_end])
                for batch_tool_call, result in zip(tool_calls[index:batch_end], results, strict=True):
                    self.session.append_tool_result(tool_call=batch_tool_call, result=result)
                    if is_successful_verification_result(batch_tool_call.name, result):
                        successful_verification = True
                index = batch_end
                continue

            result = self.execute_single(tool_call)
            self.session.append_tool_result(tool_call=tool_call, result=result)
            if is_successful_verification_result(tool_call.name, result):
                successful_verification = True
            # ask_user 这类工具本身不会继续执行副作用，而是把“需要问用户什么”包装在
            # ToolResult.data 中。这里把它转换成 AgentTurnResult 的 pending_input。
            pending_input = user_input_request_from_tool_result(
                result,
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
            )
            if pending_input is not None:
                self._emit_settlements("skipped", self.settlement.append_skipped(tool_calls[index + 1 :]))
                return ToolExecutionState(
                    task_hash_changed=task_hash_changed,
                    pending_input=pending_input,
                    successful_verification=successful_verification,
                )
            if tool_call.name == "task_boundary" and result.ok and result.data.get("should_trigger_compaction"):
                # task_boundary 是一种“语义触发”：即使上下文还没超 token 阈值，确认任务切换
                # 后也应该整理旧任务上下文，降低旧任务信息污染新任务的概率。
                self._tag_task_boundary_messages(result.data)
                task_hash_changed = True
            index += 1
        return ToolExecutionState(
            task_hash_changed=task_hash_changed,
            successful_verification=successful_verification,
        )

    async def execute_interactive_async(self, tool_calls: list[ToolCall]) -> ToolExecutionState:
        """streaming 路径下的工具执行：权限与 pending input 语义与同步版一致。"""

        task_hash_changed = False
        successful_verification = False
        index = 0
        while index < len(tool_calls):
            self._check_cancelled()
            tool_call = tool_calls[index]
            preflight = self.session.preflight_tool_call_permission(tool_call)
            if preflight is not None:
                if preflight.decision.kind == PermissionDecisionKind.DENY:
                    result = make_permission_denied_result(
                        tool_name=tool_call.name,
                        request=preflight.request,
                        decision=preflight.decision,
                    )
                    self._emit_event(
                        "denied",
                        tool_call,
                        result=result,
                        permission_request=preflight.request,
                    )
                    self.session.append_tool_result(tool_call=tool_call, result=result)
                    index += 1
                    continue
                if preflight.decision.kind == PermissionDecisionKind.ASK:
                    pending_input = self.store_pending_permission_request(
                        tool_call=tool_call,
                        request=preflight.request,
                        skipped_tool_calls=tool_calls[index + 1 :],
                    )
                    self._emit_event(
                        "permission_requested",
                        tool_call,
                        permission_request=preflight.request,
                    )
                    return ToolExecutionState(
                        task_hash_changed=task_hash_changed,
                        pending_input=pending_input,
                        successful_verification=successful_verification,
                    )

            if self.can_execute_in_parallel(tool_call):
                batch_end = self.parallel_readonly_batch_end(tool_calls, index)
                results = await self.execute_parallel_readonly_batch_async(tool_calls[index:batch_end])
                for batch_tool_call, result in zip(tool_calls[index:batch_end], results, strict=True):
                    self.session.append_tool_result(tool_call=batch_tool_call, result=result)
                    if is_successful_verification_result(batch_tool_call.name, result):
                        successful_verification = True
                index = batch_end
                continue

            result = await self.execute_single_async(tool_call)
            self.session.append_tool_result(tool_call=tool_call, result=result)
            if is_successful_verification_result(tool_call.name, result):
                successful_verification = True
            pending_input = user_input_request_from_tool_result(
                result,
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
            )
            if pending_input is not None:
                self._emit_settlements("skipped", self.settlement.append_skipped(tool_calls[index + 1 :]))
                return ToolExecutionState(
                    task_hash_changed=task_hash_changed,
                    pending_input=pending_input,
                    successful_verification=successful_verification,
                )
            if tool_call.name == "task_boundary" and result.ok and result.data.get("should_trigger_compaction"):
                self._tag_task_boundary_messages(result.data)
                task_hash_changed = True
            index += 1
        return ToolExecutionState(
            task_hash_changed=task_hash_changed,
            successful_verification=successful_verification,
        )

    def parallel_readonly_batch_end(self, tool_calls: list[ToolCall], start: int) -> int:
        end = start
        while end < len(tool_calls) and self.can_execute_in_parallel(tool_calls[end]):
            end += 1
        return end

    def can_execute_in_parallel(self, tool_call: ToolCall) -> bool:
        if tool_call.name not in self.parallel_tool_names_for_current_mode():
            return False
        preflight = self.session.preflight_tool_call_permission(tool_call)
        return preflight is None or preflight.decision.kind == PermissionDecisionKind.ALLOW

    def parallel_tool_names_for_current_mode(self) -> frozenset[str]:
        if self.session.permission_manager is not None and self.session.permission_manager.mode == PermissionMode.BYPASS:
            return BYPASS_PARALLEL_TOOL_NAMES
        return PARALLEL_READONLY_TOOL_NAMES

    def execute_single(self, tool_call: ToolCall) -> ToolResult:
        self._check_cancelled()
        self._emit_event("started", tool_call)
        with cancellation_context(self.cancellation_token):
            result = self.session.execute_tool_call(tool_call)
        self._emit_event("finished", tool_call, result=result)
        self._check_cancelled()
        return result

    async def execute_single_async(self, tool_call: ToolCall) -> ToolResult:
        self._check_cancelled()
        self._emit_event("started", tool_call)
        result = await anyio.to_thread.run_sync(self.execute_with_cancellation_context, tool_call)
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

    async def execute_parallel_readonly_batch_async(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        self._check_cancelled()
        results: list[ToolResult | None] = [None] * len(tool_calls)

        async def run_one(index: int, tool_call: ToolCall) -> None:
            results[index] = await anyio.to_thread.run_sync(self.execute_with_cancellation_context, tool_call)

        for tool_call in tool_calls:
            self._emit_event("started", tool_call)
        async with anyio.create_task_group() as task_group:
            for index, tool_call in enumerate(tool_calls):
                task_group.start_soon(run_one, index, tool_call)
        if any(result is None for result in results):
            raise RuntimeError("parallel readonly tool batch finished without all results")
        resolved = [result for result in results if result is not None]
        for tool_call, result in zip(tool_calls, resolved, strict=True):
            self._emit_event("finished", tool_call, result=result)
        self._check_cancelled()
        return resolved

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
    ) -> UserInputRequest:
        if self.session.permission_manager is None:
            raise RuntimeError("permission confirmation requires a permission manager")

        confirmation = self.session.permission_manager.build_confirmation(request)
        # UI 会看到 confirmation.payload，但恢复时不信任 UI 回传的 tool_call。真实 tool_call
        # 保存在 session.pending_permission_execution 中，避免前端篡改参数后执行。
        confirmation.payload["pending_tool_call"] = {
            "id": tool_call.id,
            "name": tool_call.name,
            "arguments": tool_call.arguments,
        }
        self.session.pending_permission_execution = PendingPermissionExecution(
            request_id=request.id,
            tool_call=tool_call,
            permission_request=request,
            skipped_tool_calls=list(skipped_tool_calls),
        )
        return confirmation

    def permission_input_request_from_pending(self, pending: PendingPermissionExecution) -> UserInputRequest:
        if self.session.permission_manager is None:
            raise RuntimeError("permission confirmation requires a permission manager")

        confirmation = self.session.permission_manager.build_confirmation(pending.permission_request)
        confirmation.payload["pending_tool_call"] = {
            "id": pending.tool_call.id,
            "name": pending.tool_call.name,
            "arguments": pending.tool_call.arguments,
        }
        return confirmation
