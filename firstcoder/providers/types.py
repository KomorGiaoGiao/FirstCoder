"""provider 层共享的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


MessageRole = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal[
    "stop",
    "tool_calls",
    "length",
    "content_filter",
    "error",
    "unknown",
    "tool_round_limit",
    "waiting_for_user_input",
]
TokenParam = Literal["max_tokens", "max_completion_tokens"]
ToolChoiceMode = Literal["auto", "none", "required"]
StreamEventKind = Literal[
    "message_started",
    "reasoning_delta",
    "text_delta",
    "tool_call_started",
    "tool_call_delta",
    "tool_call_completed",
    "message_completed",
    "error",
]


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """provider 静态能力和 OpenAI-compatible 方言开关。

    这些字段描述“请求应该怎么发”和“上层可以期待什么能力”，避免 agent loop
    为不同厂商写分支。真实 capability discovery 还没做之前，先由 preset 提供。
    """

    supports_tools: bool = True
    supports_streaming: bool = False
    supports_parallel_tool_calls: bool = False
    supports_json_mode: bool = False
    supports_vision: bool = False
    supports_reasoning: bool = False
    token_param: TokenParam = "max_tokens"


@dataclass(slots=True)
class TokenUsage:
    """provider 返回的 token 用量。

    不同 OpenAI-compatible 厂商返回字段可能不完整，所以这里允许局部为空。
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(slots=True)
class ProviderDiagnostics:
    """不会进入模型可见正文的 provider 诊断信息。"""

    reasoning: str | None = None
    raw_finish_reason: str | None = None
    warnings: list[str] = field(default_factory=list)


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


@dataclass(frozen=True, slots=True)
class ToolChoiceFunction:
    """强制模型调用指定工具。

    OpenAI-compatible wire format 需要嵌套 dict；内部只保留工具名，让 agent 不需要
    知道 provider 原始 schema。
    """

    name: str


ToolChoice = ToolChoiceMode | ToolChoiceFunction


@dataclass(slots=True)
class ChatRequest:
    """发送给 provider 的统一请求结构。"""

    messages: list[ChatMessage]
    tools: list[ToolDefinition] = field(default_factory=list)
    tool_choice: ToolChoice | None = "auto"
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
    finish_reason: FinishReason | None = None
    usage: TokenUsage | None = None
    diagnostics: ProviderDiagnostics = field(default_factory=ProviderDiagnostics)
    raw: Any | None = None


@dataclass(slots=True)
class ChatStreamEvent:
    """provider 流式输出的内部事件。

    OpenAI-compatible 的原始 chunk 只能停留在 provider 层；agent 和 UI 后续只消费
    这个受控事件结构。`response` 只在 `message_completed` 时携带最终完整结果。
    """

    kind: StreamEventKind
    text: str = ""
    tool_call: ToolCall | None = None
    tool_call_index: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str = ""
    response: ChatResponse | None = None
    diagnostics: ProviderDiagnostics = field(default_factory=ProviderDiagnostics)
