"""Agent-turn result types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from firstcoder.providers.types import ChatResponse
from firstcoder.runtime.user_input import UserInputRequest

__all__ = [
    "AgentTurnResult",
    "AgentTurnStatus",
]


class AgentTurnStatus(StrEnum):
    """一轮 agent 执行后的状态。"""

    COMPLETED = "completed"
    WAITING_FOR_USER_INPUT = "waiting_for_user_input"


@dataclass(slots=True)
class AgentTurnResult:
    """交互式 agent turn 的返回值。"""

    status: AgentTurnStatus
    response: ChatResponse | None = None
    pending_input: UserInputRequest | None = None
