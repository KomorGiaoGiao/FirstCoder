"""Agent loop budget and stop-reason types."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class AgentLoopStopReason(StrEnum):
    TOOL_ROUND_LIMIT = "tool_round_limit"
    PROVIDER_CALL_LIMIT = "provider_call_limit"
    TURN_TIMEOUT = "turn_timeout"


@dataclass(frozen=True, slots=True)
class AgentLoopLimits:
    """Configurable guardrails for one user turn."""

    max_tool_rounds: int | None = 200
    max_provider_calls: int | None = 400
    max_turn_seconds: float | None = 3600

    @classmethod
    def default(cls) -> "AgentLoopLimits":
        return cls()

    @classmethod
    def swe_lite(cls) -> "AgentLoopLimits":
        return cls(
            max_tool_rounds=60,
            max_provider_calls=100,
            max_turn_seconds=1800,
        )

    @classmethod
    def summary(cls) -> "AgentLoopLimits":
        return cls(
            max_tool_rounds=1,
            max_provider_calls=3,
            max_turn_seconds=120,
        )

    def with_max_tool_rounds(self, value: int | None) -> "AgentLoopLimits":
        return replace(self, max_tool_rounds=value)
