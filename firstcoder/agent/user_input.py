"""Agent 侧用户输入请求类型。

这一层描述“程序需要暂停并等待用户回答”的结构化请求。普通 `ask_user`
和后续权限确认都走同一个通道，但用 `kind` 区分语义，避免权限确认被模型
伪装成普通提问。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from firstcoder.providers.types import ChatResponse
from firstcoder.tools.types import ToolResult


class AgentTurnStatus(StrEnum):
    """一轮 agent 执行后的状态。"""

    COMPLETED = "completed"
    WAITING_FOR_USER_INPUT = "waiting_for_user_input"


@dataclass(slots=True)
class UserInputOption:
    """展示给用户的一个可选回答。"""

    id: str
    label: str
    description: str = ""


@dataclass(slots=True)
class UserInputRequest:
    """需要用户输入才能继续的结构化请求。"""

    id: str
    kind: Literal["ask_user", "permission_confirmation"]
    question: str
    options: list[UserInputOption] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentTurnResult:
    """交互式 agent turn 的返回值。"""

    status: AgentTurnStatus
    response: ChatResponse | None = None
    pending_input: UserInputRequest | None = None


def user_input_request_from_tool_result(
    result: ToolResult,
    *,
    tool_call_id: str,
    tool_name: str,
) -> UserInputRequest | None:
    """从工具结果恢复用户输入请求。

    工具层已经用 `ToolResult.data` 保存结构化信息。这里集中做转换，让
    agent loop 不需要知道每个工具自己的字段细节。权限确认后续会通过
    `request_type=permission_confirmation` 接入同一条路径。
    """

    if not result.data.get("requires_user_input"):
        return None

    request_type = str(result.data.get("request_type") or "ask_user")
    if request_type not in {"ask_user", "permission_confirmation"}:
        request_type = "ask_user"

    question = str(result.data.get("question") or result.content).strip()
    if not question:
        question = "需要用户输入。"

    options = _options_from_data(result.data.get("options"))
    request_id = str(result.data.get("request_id") or result.data.get("permission_request_id") or tool_call_id)
    payload = {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "tool_result_name": result.name,
    }
    for key in ("request_type", "permission_request_id", "pending_tool_call"):
        if key in result.data:
            payload[key] = result.data[key]

    return UserInputRequest(
        id=request_id,
        kind=request_type,  # type: ignore[arg-type]
        question=question,
        options=options,
        payload=payload,
    )


def _options_from_data(raw_options: object) -> list[UserInputOption]:
    if not isinstance(raw_options, list):
        return []

    options: list[UserInputOption] = []
    for index, raw_option in enumerate(raw_options, start=1):
        if isinstance(raw_option, dict):
            label = str(raw_option.get("label") or raw_option.get("id") or "").strip()
            if not label:
                continue
            option_id = str(raw_option.get("id") or index)
            description = str(raw_option.get("description") or "")
        else:
            label = str(raw_option).strip()
            if not label:
                continue
            option_id = str(index)
            description = ""
        options.append(UserInputOption(id=option_id, label=label, description=description))
    return options
