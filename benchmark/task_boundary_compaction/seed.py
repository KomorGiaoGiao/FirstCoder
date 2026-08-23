"""Deterministically seed a completed old-task transcript without synthetic tool state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import ceil

from firstcoder.agent.session import AgentSession
from firstcoder.context.models import SessionView
from firstcoder.context.token_budget import ContextBudget
from firstcoder.providers.types import ChatResponse


@dataclass(frozen=True, slots=True)
class SeededContext:
    """The safe, measurable result of writing a task-A transcript."""

    case_id: str
    task_hash: str
    message_pairs: int
    input_tokens: int
    high_watermark: int


def seed_old_task_context(
    session: AgentSession,
    *,
    case_id: str,
    target_input_tokens: int,
    estimate_budget: Callable[[SessionView], ContextBudget],
) -> SeededContext:
    """Append deterministic ordinary task-A messages below the AUTO high watermark."""

    if not case_id.strip():
        raise ValueError("case_id must not be blank")
    task_hash = session.runtime_state.active_task_hash
    if not task_hash:
        raise ValueError("active_task_hash is required before seeding")

    initial_budget = estimate_budget(session.rebuild_view())
    lower_bound = ceil(initial_budget.high_watermark * 0.75)
    if not lower_bound <= target_input_tokens < initial_budget.high_watermark:
        raise ValueError("target_input_tokens must be within the safe high_watermark range")
    if initial_budget.input_tokens >= initial_budget.high_watermark:
        raise ValueError("fixed context already reaches high_watermark")
    if initial_budget.input_tokens > target_input_tokens:
        raise ValueError("target_input_tokens is below the existing context")

    message_pairs = 0
    while True:
        current_budget = estimate_budget(session.rebuild_view())
        if current_budget.input_tokens >= target_input_tokens:
            break
        remaining_tokens = target_input_tokens - current_budget.input_tokens
        pair_tokens = min(512, remaining_tokens)
        user_tokens = max(1, pair_tokens // 2)
        assistant_tokens = max(1, pair_tokens - user_tokens)
        message_pairs += 1
        session.append_user_message(_seed_text(case_id, message_pairs, "user", user_tokens))
        session.append_assistant_response(
            ChatResponse(
                provider="benchmark-seed",
                model="deterministic",
                content=_seed_text(case_id, message_pairs, "assistant", assistant_tokens),
                finish_reason="stop",
            )
        )
        current_budget = estimate_budget(session.rebuild_view())
        if current_budget.input_tokens >= current_budget.high_watermark:
            raise ValueError("seed unexpectedly reached high_watermark")

    final_budget = estimate_budget(session.rebuild_view())
    if final_budget.input_tokens < lower_bound or final_budget.input_tokens >= final_budget.high_watermark:
        raise ValueError("seed did not finish within the safe high_watermark range")
    return SeededContext(
        case_id=case_id,
        task_hash=task_hash,
        message_pairs=message_pairs,
        input_tokens=final_budget.input_tokens,
        high_watermark=final_budget.high_watermark,
    )


def _seed_text(case_id: str, pair_index: int, role: str, token_count: int) -> str:
    header = f"[任务 A 已完成 | case={case_id} | pair={pair_index} | role={role}]\n"
    evidence = (
        "任务 A 的确定性历史：已检查输入、记录约束、验证预期，并保留与任务 B 无关的实现细节。"
    )
    body_length = max(1, token_count * 4)
    repeated = (evidence * (body_length // len(evidence) + 1))[:body_length]
    return header + repeated
