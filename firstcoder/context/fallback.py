"""LLM compact 失败后的有限兜底策略。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

FallbackAction = Literal["stronger_programmatic", "retry_l4_stronger_summary", "fail"]


@dataclass(frozen=True, slots=True)
class FallbackStep:
    """一次 fallback 尝试的结构化记录。"""

    step: int
    reason: str
    action: FallbackAction
    before_tokens: int
    after_tokens: int
    status: Literal["success", "failed", "skipped"]
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CompactFallbackPolicy:
    """根据 L4 失败原因选择有限 fallback。"""

    def action_for(self, reason: str | None) -> FallbackAction:
        if reason == "prompt_too_long":
            return "stronger_programmatic"
        if reason in {"timeout", "no_summary"}:
            return "retry_l4_stronger_summary"
        return "fail"
