"""把内部会话事实投影成 provider 请求消息。"""

from __future__ import annotations

from firstcoder.context.checkpoint import Checkpoint, CheckpointIndex, checkpoint_summary_content
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.tool_sequence import validate_tool_call_sequence
from firstcoder.providers.types import ChatMessage, ToolCall


class InvalidCheckpointBoundaryError(ValueError):
    """checkpoint tail 边界会生成 provider 无法接受的消息序列。"""


class ContextBuilder:
    """只负责投影，不负责压缩、总结、落盘或任务边界判断。

    `SessionView` 是 FirstCoder 自己的事实账本，不等于 provider 请求格式。ContextBuilder
    的职责就是在每次调用模型前，把当前可见历史转换成 `ChatMessage` 列表：

    - system prefix 由 AgentSession 传入。
    - 如果有 checkpoint，先插入一条“旧历史摘要”。
    - 再保留 checkpoint tail 之后的真实消息。
    - 最后校验 tool_call/tool_result 序列，避免 provider 拒绝请求。
    """

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
            # checkpoint 不删除原始历史，只改变本次 provider 请求看到的上下文：旧历史用摘要
            # 表示，tail 部分保留原文，便于模型继续当前任务。
            messages.append(ChatMessage(role="user", content=checkpoint_summary_content(active_checkpoint)))

        tail_messages = self._tail_messages(view, checkpoint=active_checkpoint)
        # provider 对 tool calling 序列很严格：assistant tool_call 后必须紧跟对应 tool result。
        # 压缩/checkpoint 不能把这个配对切断。
        validate_tool_call_sequence(tail_messages)
        if _has_trimmed_text(tail_messages):
            # One aggregate marker keeps the provider informed without adding a
            # synthetic message for each forgotten part or splitting a tool
            # transaction.  It belongs after the checkpoint and before the
            # real tail, therefore it cannot become an orphan tool result.
            messages.append(ChatMessage(role="user", content="[Earlier dialogue trimmed]"))
        latest_user_message_id = _latest_user_message_id(tail_messages)
        for message in tail_messages:
            projected = self._project_message(
                message,
                preserve_trimmed_text=message.id == latest_user_message_id,
            )
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
                # tail 不能从 tool message 开始，否则 provider 会看到一个没有前置 assistant
                # tool_call 的孤立 tool_result。
                _validate_tail_boundary(tail)
                return tail
        raise InvalidCheckpointBoundaryError(
            f"checkpoint tail_start_message_id not found: {checkpoint.tail_start_message_id}",
        )

    def _project_message(
        self,
        message: AgentMessage,
        *,
        preserve_trimmed_text: bool = False,
    ) -> list[ChatMessage]:
        if message.role == "system_meta":
            # system_meta 是内部状态，不应该作为普通对话消息发给 provider。
            return []

        if message.role == "tool":
            # tool message 可能包含普通工具结果，也可能是 archive placeholder。二者都要用
            # role=tool 回给模型，并带上原始 tool_call_id。
            return [
                _project_tool_part(part)
                for part in message.parts
                if part.kind in {"tool_result", "archive_placeholder"}
            ]

        if message.role == "assistant":
            projected = _project_assistant_message(
                message,
                preserve_trimmed_text=preserve_trimmed_text
                or any(part.kind == "tool_call" for part in message.parts),
            )
            # A fully trimmed ordinary assistant turn must not become a blank
            # provider message.  Assistant messages with tool calls are still
            # emitted even when their visible text happens to be empty.
            return [projected] if projected.content or projected.tool_calls else []

        if message.role == "user":
            content = _join_visible_text(message.parts, preserve_trimmed_text=preserve_trimmed_text)
            # basis_message_id 是给 task_boundary 工具用的锚点。模型只能引用真实存在的
            # message id，程序侧再据此生成稳定 task hash。
            return [ChatMessage(role="user", content=_with_basis_message_id(message.id, content))] if content else []

        return []


def _project_assistant_message(
    message: AgentMessage,
    *,
    preserve_trimmed_text: bool = False,
) -> ChatMessage:
    """把内部 assistant parts 合并成 provider assistant message。"""

    text_parts = [
        part.content
        for part in message.parts
        if part.kind == "text"
        and (preserve_trimmed_text or _is_visible_text_part(part))
        and part.content
    ]
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
    """把内部工具结果投影成 provider 需要的 role=tool 消息。"""

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


def _join_visible_text(parts: list[MessagePart], *, preserve_trimmed_text: bool = False) -> str:
    return "\n".join(
        part.content
        for part in parts
        if part.kind in {"text", "archive_placeholder"}
        and (preserve_trimmed_text or _is_visible_text_part(part))
        and part.content
    )


def _has_trimmed_text(messages: list[AgentMessage]) -> bool:
    return any(
        part.kind == "text" and part.metadata.get("compaction_state") == "trimmed"
        for message in messages
        for part in message.parts
    )


def _is_visible_text_part(part: MessagePart) -> bool:
    return part.metadata.get("compaction_state") != "trimmed"


def _latest_user_message_id(messages: list[AgentMessage]) -> str | None:
    for message in reversed(messages):
        if message.role == "user":
            return message.id
    return None


def _with_basis_message_id(message_id: str, content: str) -> str:
    return f"[context: basis_message_id={message_id}]\n{content}"
