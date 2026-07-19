"""Tool calling 协议写入与校验 helper。"""

from __future__ import annotations

from firstcoder.context.identity import new_part_id
from firstcoder.context.models import MessagePart
from firstcoder.context.tool_sequence import InvalidToolCallSequenceError, validate_tool_call_sequence
from firstcoder.context.writer import tool_call_to_part
from firstcoder.providers.types import ChatResponse, ToolCall
from firstcoder.tools.types import ToolResult


def assistant_response_to_parts(*, message_id: str, response: ChatResponse) -> list[MessagePart]:
    """把 provider assistant response 转成内部 parts。

    provider 协议里的 assistant 消息可以同时包含可见文本和 tool_calls。这里集中做转换，
    避免不同调用方手写 metadata 导致 tool_call_id / arguments 字段漂移。
    """

    parts: list[MessagePart] = []
    if response.content:
        parts.append(MessagePart(id=new_part_id(), message_id=message_id, kind="text", content=response.content))
    for tool_call in response.tool_calls:
        parts.append(tool_call_to_part(message_id=message_id, tool_call=tool_call))
    return parts


def tool_result_to_part(*, message_id: str, tool_call: ToolCall, result: ToolResult) -> MessagePart:
    """把 ToolResult 转成 role=tool 消息里的单个 part。"""

    return MessagePart(
        id=new_part_id(),
        message_id=message_id,
        kind="tool_result",
        content=result.content,
        metadata={
            "tool_call_id": tool_call.id,
            "tool_name": tool_call.name,
            "ok": result.ok,
            "data": result.data,
            "error": result.error,
        },
    )
