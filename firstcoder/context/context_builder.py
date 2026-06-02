"""把内部会话事实投影成 provider 请求消息。"""

from __future__ import annotations

from firstcoder.context.checkpoint import Checkpoint, CheckpointIndex, checkpoint_summary_content
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.tool_sequence import validate_tool_call_sequence
from firstcoder.providers.types import ChatMessage, ToolCall


class InvalidCheckpointBoundaryError(ValueError):
    """checkpoint tail 边界会生成 provider 无法接受的消息序列。"""


class ContextBuilder:
    """只负责投影，不负责压缩、总结、落盘或任务边界判断。"""

    def build_provider_messages(
        self,
        view: SessionView,
        *,
        system_prefix: list[ChatMessage] | None = None,
        checkpoint: Checkpoint | None = None,
    ) -> list[ChatMessage]:
        active_checkpoint = checkpoint or CheckpointIndex(view.checkpoints).latest()
        messages = list(system_prefix or [])
        if active_checkpoint is not None:
            messages.append(ChatMessage(role="user", content=checkpoint_summary_content(active_checkpoint)))

        tail_messages = self._tail_messages(view, checkpoint=active_checkpoint)
        validate_tool_call_sequence(tail_messages)
        for message in tail_messages:
            projected = self._project_message(message)
            messages.extend(projected)
        return messages

    def _tail_messages(
        self,
        view: SessionView,
        *,
        checkpoint: Checkpoint | None,
    ) -> list[AgentMessage]:
        if checkpoint is None:
            return view.messages

        for index, message in enumerate(view.messages):
            if message.id == checkpoint.tail_start_message_id:
                tail = view.messages[index:]
                _validate_tail_boundary(tail)
                return tail
        raise InvalidCheckpointBoundaryError(
            f"checkpoint tail_start_message_id not found: {checkpoint.tail_start_message_id}",
        )

    def _project_message(self, message: AgentMessage) -> list[ChatMessage]:
        if message.role == "system_meta":
            return []

        if message.role == "tool":
            return [
                _project_tool_part(part)
                for part in message.parts
                if part.kind in {"tool_result", "archive_placeholder"}
            ]

        if message.role == "assistant":
            return [_project_assistant_message(message)]

        if message.role == "user":
            content = _join_visible_text(message.parts)
            return [ChatMessage(role="user", content=content)] if content else []

        return []


def _project_assistant_message(message: AgentMessage) -> ChatMessage:
    text_parts = [part.content for part in message.parts if part.kind == "text" and part.content]
    tool_calls = [
        ToolCall(
            id=str(part.metadata["tool_call_id"]),
            name=str(part.metadata["tool_name"]),
            arguments=part.metadata.get("arguments", {}),
        )
        for part in message.parts
        if part.kind == "tool_call"
    ]
    return ChatMessage(role="assistant", content="\n".join(text_parts), tool_calls=tool_calls)


def _project_tool_part(part: MessagePart) -> ChatMessage:
    return ChatMessage(
        role="tool",
        content=part.content,
        name=str(part.metadata.get("tool_name")) if part.metadata.get("tool_name") else None,
        tool_call_id=str(part.metadata["tool_call_id"]),
    )


def _validate_tail_boundary(messages: list[AgentMessage]) -> None:
    if not messages:
        return
    first = messages[0]
    if first.role == "tool":
        raise InvalidCheckpointBoundaryError(
            "checkpoint tail starts with orphan tool result; move tail_start_message_id "
            "to the assistant tool_call before this tool result",
        )


def _join_visible_text(parts: list[MessagePart]) -> str:
    return "\n".join(part.content for part in parts if part.kind in {"text", "archive_placeholder"} and part.content)
