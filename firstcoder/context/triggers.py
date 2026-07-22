"""上下文压缩阈值与触发判断。"""

from __future__ import annotations

from dataclasses import dataclass

from firstcoder.context.checkpoint import CheckpointIndex, checkpoint_summary_content
from firstcoder.context.models import AgentMessage, SessionView
from firstcoder.context.token_budget import estimate_text_tokens


@dataclass(frozen=True, slots=True)
class ContextCompactionConfig:
    """上下文压缩相关阈值的集中配置。

    这一层只描述“什么时候应该 compact、目标预算是多少”。具体 L1-L4 怎么压缩仍由
    `CompactionPipeline` 和 `LlmCompactService` 负责。
    """

    auto_compact_threshold: int = 32_000
    target_tokens: int = 24_000
    blocking_target_tokens: int | None = None
    task_switch_target_tokens: int | None = None
    l2_result_target_tokens: int = 800
    large_tool_result_tokens: int = 1_200
    max_turn_tool_result_tokens: int = 4_000
    max_tail_messages: int = 120
    max_tail_tokens: int = 28_000
    cold_turn_distance: int = 8
    cold_preview_chars: int = 160
    reserved_output_tokens: int = 4_096

    def target_for_trigger(self, trigger: str) -> int:
        if trigger == "prompt_too_long" and self.blocking_target_tokens is not None:
            return self.blocking_target_tokens
        if trigger == "task_hash_changed":
            if self.task_switch_target_tokens is not None:
                return self.task_switch_target_tokens
            return max(1, self.target_tokens * 2 // 3)
        return self.target_tokens


@dataclass(frozen=True, slots=True)
class ContextTriggerDecision:
    should_compact: bool
    reason: str
    estimated_tokens: int
    target_tokens: int


@dataclass(frozen=True, slots=True)
class TriggerScope:
    tail_messages: list[AgentMessage]
    checkpoint_summary_tokens: int = 0

    @property
    def estimated_tokens(self) -> int:
        return self.checkpoint_summary_tokens + _estimate_messages_tokens(self.tail_messages)


def evaluate_context_triggers(
    view: SessionView,
    config: ContextCompactionConfig,
    *,
    estimated_tokens_override: int | None = None,
) -> ContextTriggerDecision:
    scope = _trigger_scope(view)
    estimated_tokens = estimated_tokens_override if estimated_tokens_override is not None else scope.estimated_tokens
    target_tokens = config.target_tokens

    if estimated_tokens >= config.auto_compact_threshold:
        return ContextTriggerDecision(True, "token_threshold", estimated_tokens, target_tokens)

    if _has_large_tool_result(scope.tail_messages, config=config):
        return ContextTriggerDecision(True, "large_tool_result", estimated_tokens, target_tokens)

    if _turn_tool_result_tokens(scope.tail_messages) >= config.max_turn_tool_result_tokens:
        return ContextTriggerDecision(True, "turn_tool_results", estimated_tokens, target_tokens)

    if len(scope.tail_messages) > config.max_tail_messages:
        return ContextTriggerDecision(True, "tail_message_count", estimated_tokens, target_tokens)

    if estimated_tokens >= config.max_tail_tokens:
        return ContextTriggerDecision(True, "tail_token_count", estimated_tokens, target_tokens)

    return ContextTriggerDecision(False, "under_threshold", estimated_tokens, target_tokens)


def _estimate_view_tokens(view: SessionView) -> int:
    return sum(estimate_text_tokens(part.content) for message in view.messages for part in message.parts)


def _trigger_scope(view: SessionView) -> TriggerScope:
    checkpoint = CheckpointIndex(view.checkpoints).latest()
    if checkpoint is None:
        return TriggerScope(tail_messages=list(view.messages))

    for index, message in enumerate(view.messages):
        if message.id == checkpoint.tail_start_message_id:
            return TriggerScope(
                tail_messages=list(view.messages[index:]),
                checkpoint_summary_tokens=estimate_text_tokens(checkpoint_summary_content(checkpoint)),
            )
    raise ValueError(f"latest checkpoint tail_start_message_id not found: {checkpoint.tail_start_message_id}")


def _estimate_messages_tokens(messages: list[AgentMessage]) -> int:
    return sum(estimate_text_tokens(part.content) for message in messages for part in message.parts)


def _has_large_tool_result(messages: list[AgentMessage], *, config: ContextCompactionConfig) -> bool:
    for message in messages:
        if message.role != "tool":
            continue
        for part in message.parts:
            if part.kind == "tool_result" and estimate_text_tokens(part.content) >= config.large_tool_result_tokens:
                return True
    return False


def _turn_tool_result_tokens(messages: list[AgentMessage]) -> int:
    current_turn_id: object | None = None
    current_total = 0
    max_total = 0
    for message in messages:
        for part in message.parts:
            if part.kind != "tool_result":
                continue
            turn_id = part.metadata.get("turn_id") or message.id
            if turn_id != current_turn_id:
                max_total = max(max_total, current_total)
                current_turn_id = turn_id
                current_total = 0
            current_total += estimate_text_tokens(part.content)
    return max(max_total, current_total)
