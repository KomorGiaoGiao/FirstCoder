"""会话事件写入 helper。

JSONL store 的底层接口故意保持简单，只负责 append/list/rebuild。上层如果到处手写
payload，tool_call/tool_result 这类 provider 协议边界很容易漂移；writer 把常见事件写入
集中起来，后续正式事件 schema 也优先在这里演进。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from firstcoder.context.compaction import CompactionEvent
from firstcoder.context.events import SessionEvent
from firstcoder.context.identity import new_event_id, new_message_id, new_part_id
from firstcoder.context.llm_compact import LlmCompactEvent
from firstcoder.context.models import MessagePart
from firstcoder.context.store import JsonlSessionStore
from firstcoder.providers.types import ChatResponse, ToolCall
from firstcoder.tools.types import ToolResult


class SessionEventWriter:
    """为单个 session 追加结构化事件。"""

    def __init__(self, *, store: JsonlSessionStore, session_id: str) -> None:
        self.store = store
        self.session_id = session_id

    def append_session_created(self, **metadata: Any) -> None:
        payload = {"session_id": self.session_id}
        payload.update(metadata)
        self.store.append_event(
            SessionEvent(
                id=new_event_id(),
                session_id=self.session_id,
                type="session_created",
                payload=payload,
            )
        )

    def append_user_message(self, content: str, *, metadata: dict[str, Any] | None = None) -> str:
        message_id = new_message_id()
        part = MessagePart(id=new_part_id(), message_id=message_id, kind="text", content=content)
        self._append_message_event(
            "user_message",
            message_id=message_id,
            parts=[part],
            metadata=metadata,
        )
        return message_id

    def append_assistant_response(self, response: ChatResponse) -> str:
        message_id = new_message_id()
        parts: list[MessagePart] = []
        if response.content:
            parts.append(MessagePart(id=new_part_id(), message_id=message_id, kind="text", content=response.content))
        for tool_call in response.tool_calls:
            parts.append(_tool_call_part(message_id=message_id, tool_call=tool_call))
        self._append_message_event(
            "assistant_message",
            message_id=message_id,
            parts=parts,
            metadata={
                "provider": response.provider,
                "model": response.model,
                "finish_reason": response.finish_reason,
            },
        )
        return message_id

    def append_assistant_parts(
        self,
        parts: list[MessagePart],
        *,
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> str:
        """写入已经由 agent 层转换好的 assistant parts。"""

        message_id = message_id or new_message_id()
        self._append_message_event(
            "assistant_message",
            message_id=message_id,
            parts=parts,
            metadata=metadata,
        )
        return message_id

    def append_tool_result(self, *, tool_call: ToolCall, result: ToolResult) -> str:
        message_id = new_message_id()
        part = MessagePart(
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
        self._append_message_event("tool_result", message_id=message_id, parts=[part])
        return message_id

    def append_tool_result_part(self, part: MessagePart, *, message_id: str | None = None) -> str:
        """写入已经由 agent 层转换好的 tool_result part。"""

        message_id = message_id or part.message_id
        self._append_message_event("tool_result", message_id=message_id, parts=[part])
        return message_id

    def append_compaction_completed(
        self,
        *,
        trigger: str,
        target_tokens: int,
        event: CompactionEvent,
    ) -> None:
        self.store.append_event(
            SessionEvent(
                id=new_event_id(),
                session_id=self.session_id,
                type="compaction_completed",
                payload={
                    "trigger": trigger,
                    "target_tokens": target_tokens,
                    "event": asdict(event),
                },
            )
        )

    def append_llm_compaction_completed(
        self,
        *,
        trigger: str,
        target_tokens: int,
        event: LlmCompactEvent,
    ) -> None:
        self.store.append_event(
            SessionEvent(
                id=new_event_id(),
                session_id=self.session_id,
                type="llm_compaction_completed",
                payload={
                    "trigger": trigger,
                    "target_tokens": target_tokens,
                    "event": asdict(event),
                },
            )
        )

    def _append_message_event(
        self,
        event_type: str,
        *,
        message_id: str,
        parts: list[MessagePart],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.store.append_event(
            SessionEvent(
                id=new_event_id(),
                session_id=self.session_id,
                type=event_type,
                payload={
                    "message_id": message_id,
                    "parts": [part.to_dict() for part in parts],
                    "metadata": metadata or {},
                },
            )
        )


def _tool_call_part(*, message_id: str, tool_call: ToolCall) -> MessagePart:
    return MessagePart(
        id=new_part_id(),
        message_id=message_id,
        kind="tool_call",
        content="",
        metadata={
            "tool_call_id": tool_call.id,
            "tool_name": tool_call.name,
            "arguments": tool_call.arguments,
        },
    )
