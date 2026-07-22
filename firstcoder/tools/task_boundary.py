"""`task_boundary` 工具。

这个工具只让模型提交任务边界判断，不让模型提交 hash。hash 由 context 层根据
session_id 和 basis_message_id 稳定生成，避免不同模型输出格式不一致。
"""

from __future__ import annotations

from typing import Any

from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.context.task_boundary import TaskBoundaryDecision, TaskBoundaryService
from firstcoder.providers.types import ToolDefinition
from firstcoder.tools.descriptions import apply_agent_tool_description
from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result


def create_task_boundary_tool(
    state: SessionRuntimeState,
    *,
    required_stable_count: int = 2,
    service: TaskBoundaryService | None = None,
) -> Tool:
    """创建任务边界工具。

    `state` 是当前会话的运行期状态，所以这个工具不适合放进无状态的默认工具注册。
    agent loop 后续应在创建单次会话工具集时注入。
    """

    boundary_service = service or TaskBoundaryService(required_stable_count=required_stable_count)

    def task_boundary(decision: str, basis_message_id: str, **kwargs: Any) -> ToolResult:
        """报告当前消息是否开启了新任务。"""

        if "task_hash" in kwargs or "hash" in kwargs:
            return make_error_result("task_boundary", "task_boundary 不接受模型传入 hash")
        if kwargs:
            return make_error_result(
                "task_boundary",
                f"未知参数：{', '.join(sorted(kwargs))}",
                arguments=kwargs,
            )

        try:
            observation = boundary_service.observe(
                state,
                decision=decision,
                basis_message_id=basis_message_id,
            )
        except ValueError as exc:
            message = str(exc)
            if "basis_message_id" in message:
                return make_error_result(
                    "task_boundary",
                    message,
                    basis_message_id=basis_message_id,
                )
            return make_error_result(
                "task_boundary",
                "decision 必须是 same、new 或 uncertain",
                decision=decision,
            )

        content = _format_observation(observation.confirmed_change, observation.should_trigger_compaction)
        return make_text_result(
            "task_boundary",
            content,
            decision=observation.decision.value,
            basis_message_id=observation.basis_message_id,
            candidate_hash=observation.candidate_hash,
            candidate_basis_message_id=observation.candidate_basis_message_id,
            active_task_hash=observation.active_task_hash,
            confirmed_change=observation.confirmed_change,
            should_trigger_compaction=observation.should_trigger_compaction,
            triggered_compaction=observation.triggered_compaction,
            stable_count=observation.stable_count,
            required_stable_count=observation.required_stable_count,
            confirmation_reason=observation.confirmation_reason,
            event_version=observation.event_version,
            strategy_version=observation.strategy_version,
            created_at=observation.created_at,
        )

    return apply_agent_tool_description(
        Tool(
            definition=ToolDefinition(
                name="task_boundary",
                description="判断当前消息是否开启新任务；只提交 decision 和 basis_message_id。",
                parameters={
                    "type": "object",
                    "properties": {
                        "decision": {
                            "type": "string",
                            "enum": [
                                TaskBoundaryDecision.SAME.value,
                                TaskBoundaryDecision.NEW.value,
                                TaskBoundaryDecision.UNCERTAIN.value,
                            ],
                        },
                        "basis_message_id": {"type": "string"},
                    },
                    "required": ["decision", "basis_message_id"],
                },
            ),
            executor=task_boundary,
        )
    )


def _format_observation(confirmed_change: bool, should_trigger_compaction: bool) -> str:
    if confirmed_change and should_trigger_compaction:
        return "已确认任务切换，应触发上下文压缩 pipeline。"
    return "任务边界观察已记录，暂不触发压缩。"
