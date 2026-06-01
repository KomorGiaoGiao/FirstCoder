"""provider 层共享的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


MessageRole = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ChatMessage:
    """agent 内部使用的统一消息结构。

    不同厂商的消息格式不完全一致，所以项目内部先使用自己的结构。
    provider 负责把这个结构转换成各家 SDK 需要的请求格式。
    """

    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass(slots=True)
class ToolDefinition:
    """模型可调用工具的统一描述。

    `parameters` 使用 JSON Schema 风格，方便转换到 OpenAI tool calling、
    Anthropic tool use，以及后续其他 provider 的工具格式。
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolCall:
    """模型返回的一次工具调用请求。"""

    id: str
    name: str
    arguments: dict[str, Any] | str


@dataclass(slots=True)
class ChatRequest:
    """发送给 provider 的统一请求结构。"""

    messages: list[ChatMessage]
    tools: list[ToolDefinition] = field(default_factory=list)
    tool_choice: str | dict[str, Any] | None = "auto"
    temperature: float | None = None
    max_tokens: int | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChatResponse:
    """provider 返回给 agent 主循环的统一响应结构。"""

    provider: str
    model: str
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    raw: Any | None = None
