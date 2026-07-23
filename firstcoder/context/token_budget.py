"""上下文 token 预算的集中估算。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from firstcoder.context.budget_defaults import DEFAULT_CONTEXT_WINDOW, DEFAULT_OUTPUT_RESERVE
from firstcoder.providers.types import ChatMessage, ToolDefinition


IMAGE_INPUT_TOKEN_ESTIMATE = 1_024


@dataclass(frozen=True, slots=True)
class ContextBudget:
    context_window: int
    output_reserve: int
    input_capacity: int
    fixed_tokens: int
    history_tokens: int
    input_tokens: int
    high_watermark: int
    low_watermark: int
    source: Literal["configured", "assumed"]


def estimate_text_tokens(text: str) -> int:
    """第一版使用字符数近似 token。

    这里有意不绑定具体 tokenizer，避免 context 层过早依赖 provider。
    """

    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def estimate_chat_request_tokens(
    *,
    messages: list[ChatMessage],
    tools: list[ToolDefinition],
    reserved_output_tokens: int = 0,
) -> int:
    """Estimate the model-facing request, including schema and output reserve.

    This remains provider-neutral and deliberately uses the same cheap text
    heuristic as the rest of the context layer.  Unlike a tail-only estimate,
    it includes all request material that occupies the provider window.
    """

    message_tokens = sum(_estimate_chat_message_tokens(message) for message in messages)
    tool_tokens = _estimate_tool_definition_tokens(tools)
    return message_tokens + tool_tokens + max(0, reserved_output_tokens)


def build_context_budget(
    *,
    messages: list[ChatMessage],
    tools: list[ToolDefinition],
    context_window: int | None,
    max_output_tokens: int | None,
) -> ContextBudget:
    resolved_window = DEFAULT_CONTEXT_WINDOW if context_window is None else context_window
    output_reserve = DEFAULT_OUTPUT_RESERVE if max_output_tokens is None else max_output_tokens
    if resolved_window <= 0 or output_reserve <= 0:
        raise ValueError("context window and output reserve must be positive")

    usable_window = int(resolved_window * 0.95)
    input_capacity = usable_window - output_reserve
    if input_capacity <= 0:
        raise ValueError("output reserve must be smaller than usable context window")

    fixed_tokens = sum(_estimate_chat_message_tokens(message) for message in messages if message.role == "system")
    fixed_tokens += _estimate_tool_definition_tokens(tools)
    history_tokens = sum(_estimate_chat_message_tokens(message) for message in messages if message.role != "system")
    high_watermark = int(input_capacity * 0.90)
    low_watermark = int(input_capacity * 0.72)
    if low_watermark >= high_watermark:
        raise ValueError("low watermark must be below high watermark")

    return ContextBudget(
        context_window=resolved_window,
        output_reserve=output_reserve,
        input_capacity=input_capacity,
        fixed_tokens=fixed_tokens,
        history_tokens=history_tokens,
        input_tokens=fixed_tokens + history_tokens,
        high_watermark=high_watermark,
        low_watermark=low_watermark,
        source="configured" if context_window is not None else "assumed",
    )


def _estimate_chat_message_tokens(message: ChatMessage) -> int:
    tokens = estimate_text_tokens(message.content)
    tokens += estimate_text_tokens(message.name or "")
    tokens += estimate_text_tokens(message.tool_call_id or "")
    tokens += sum(
        estimate_text_tokens(call.name + json.dumps(call.arguments, ensure_ascii=False, sort_keys=True))
        for call in message.tool_calls
    )
    tokens += sum(
        IMAGE_INPUT_TOKEN_ESTIMATE
        for part in message.content_parts or []
        if part.type == "image"
    )
    return tokens


def _estimate_tool_definition_tokens(tools: list[ToolDefinition]) -> int:
    return sum(
        estimate_text_tokens(
            json.dumps(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        for tool in tools
    )
