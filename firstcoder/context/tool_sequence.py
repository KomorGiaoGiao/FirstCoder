"""Tool calling 历史序列校验。"""

from __future__ import annotations

from firstcoder.context.models import AgentMessage, MessagePart


class InvalidToolCallSequenceError(ValueError):
    """会话历史中的 tool_call / tool_result 序列无法投影给 provider。"""


def validate_tool_call_sequence(messages: list[AgentMessage]) -> None:
    """校验 provider 可接受的 tool_call / tool_result 原子序列。

    OpenAI-compatible 和 Anthropic 都要求 tool result 能回溯到前面的 assistant
    tool_call。上下文压缩、checkpoint tail 或手写事件一旦留下孤立 tool result，
    这里会在投影前报错，而不是把坏请求发给 provider。
    """

    pending_tool_call_ids: set[str] = set()
    for message in messages:
        if message.role == "assistant":
            _raise_if_pending(pending_tool_call_ids)
            for part in message.parts:
                if part.kind == "tool_call":
                    pending_tool_call_ids.add(_required_metadata(part, "tool_call_id"))
            continue

        if message.role != "tool":
            _raise_if_pending(pending_tool_call_ids)
            continue

        for part in message.parts:
            if part.kind not in {"tool_result", "archive_placeholder"}:
                continue
            tool_call_id = _required_metadata(part, "tool_call_id")
            if tool_call_id not in pending_tool_call_ids:
                raise InvalidToolCallSequenceError(
                    f"orphan tool result without matching assistant tool_call: {tool_call_id}",
                )
            pending_tool_call_ids.remove(tool_call_id)
    _raise_if_pending(pending_tool_call_ids)


def _required_metadata(part: MessagePart, key: str) -> str:
    value = part.metadata.get(key)
    if value is None or value == "":
        raise InvalidToolCallSequenceError(f"{part.kind} missing metadata.{key}")
    return str(value)


def _raise_if_pending(pending_tool_call_ids: set[str]) -> None:
    if pending_tool_call_ids:
        pending = ", ".join(sorted(pending_tool_call_ids))
        raise InvalidToolCallSequenceError(f"assistant tool_call missing matching tool result: {pending}")
