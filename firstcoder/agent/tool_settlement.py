"""工具调用结果的持久化收尾。"""

from __future__ import annotations

from dataclasses import dataclass

from firstcoder.agent.session import AgentSession
from firstcoder.providers.types import ToolCall
from firstcoder.tools.types import ToolResult, make_error_result


@dataclass(frozen=True, slots=True)
class SettledToolCall:
    tool_call: ToolCall
    result: ToolResult


class ToolCallSettlement:
    """保持 provider 历史中 tool_call / tool_result 的闭合关系。"""

    def __init__(self, session: AgentSession) -> None:
        self.session = session

    def append_skipped(self, tool_calls: list[ToolCall]) -> list[SettledToolCall]:
        settled = [
            SettledToolCall(
                tool_call=tool_call,
                result=make_error_result(
                    tool_call.name,
                    "已暂停等待用户输入，跳过同批次后续工具调用。",
                    skipped_due_to_user_input=True,
                ),
            )
            for tool_call in tool_calls
        ]
        for item in settled:
            self.session.append_tool_result(tool_call=item.tool_call, result=item.result)
        return settled

    def append_interrupted_tail(self) -> list[SettledToolCall]:
        tool_calls = self.session.append_interrupted_tool_results()
        return [SettledToolCall(tool_call=call, result=interrupted_result(call)) for call in tool_calls]

    def repair_before_provider_request(self) -> list[SettledToolCall]:
        if self.session.pending_permission_execution is not None:
            return []
        return self.append_interrupted_tail()


def interrupted_result(tool_call: ToolCall) -> ToolResult:
    return make_error_result(
        tool_call.name,
        "工具执行被用户中断；结果未知，操作可能尚未执行、部分执行，或已在后台继续。",
        interrupted=True,
        execution_outcome="unknown",
    )
