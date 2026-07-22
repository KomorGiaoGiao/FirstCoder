"""上下文 token 预算的集中估算。"""

from __future__ import annotations

import json

from firstcoder.providers.types import ChatMessage, ToolDefinition


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

    message_tokens = sum(
        estimate_text_tokens(message.content) + sum(estimate_text_tokens(call.name + json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)) for call in message.tool_calls)
        for message in messages
    )
    tool_tokens = sum(
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
    return message_tokens + tool_tokens + max(0, reserved_output_tokens)
