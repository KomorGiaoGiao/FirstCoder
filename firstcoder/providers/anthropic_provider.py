"""Anthropic provider 实现。"""

from __future__ import annotations

from typing import Any

from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import ProviderError, ProviderErrorKind
from firstcoder.providers.tool_adapters import to_anthropic_tool
from firstcoder.providers.types import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    FinishReason,
    ProviderCapabilities,
    ProviderDiagnostics,
    ToolCall,
)


def _read_field(value: Any, name: str, default: Any = None) -> Any:
    """同时兼容 Anthropic SDK 对象和测试 dict 的字段读取。"""

    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class AnthropicProvider(ChatProvider):
    """实验性的 Anthropic Messages API provider。

    当前项目主线只保证 OpenAI-compatible provider。这个实现保留为协议学习和
    后续扩展占位，不承诺支持 Anthropic 原生 streaming、thinking 或 cache_control。
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        client: Any | None = None,
    ) -> None:
        self._model = model

        if client is not None:
            self._client = client
        else:
            from anthropic import Anthropic

            self._client = Anthropic(api_key=api_key)

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_streaming=False, supports_forced_tool_choice=False)

    def complete(self, request: ChatRequest) -> ChatResponse:
        """调用 Anthropic Messages API，并转换为统一响应。"""

        params: dict[str, Any] = {
            "model": self._model,
            "messages": self._to_anthropic_messages(request.messages),
            # Anthropic 要求 max_tokens 必填；没有传入时给一个保守默认值。
            "max_tokens": request.max_tokens or 4096,
        }

        system_prompt = self._collect_system_prompt(request.messages)
        if system_prompt:
            params["system"] = system_prompt
        if request.tools:
            params["tools"] = [to_anthropic_tool(tool) for tool in request.tools]
            if request.tool_choice is not None and request.tool_choice != "auto":
                raise ProviderError(
                    ProviderErrorKind.CONFIG_ERROR,
                    "AnthropicProvider 仍是实验性实现，当前只支持 tool_choice=None 或 auto",
                )
        if request.temperature is not None:
            params["temperature"] = request.temperature

        response = self._client.messages.create(**params)
        content_blocks = _read_field(response, "content", []) or []
        raw_finish_reason = _read_field(response, "stop_reason")

        return ChatResponse(
            provider=self.name,
            model=_read_field(response, "model", self._model),
            content=self._collect_text(content_blocks),
            tool_calls=self._parse_tool_calls(content_blocks),
            finish_reason=_normalize_stop_reason(raw_finish_reason),
            diagnostics=ProviderDiagnostics(raw_finish_reason=raw_finish_reason),
            raw=response,
        )

    @staticmethod
    def _collect_system_prompt(messages: list[ChatMessage]) -> str:
        """Anthropic 把 system prompt 放在独立字段，而不是 messages 列表里。"""

        return "\n\n".join(message.content for message in messages if message.role == "system")

    @staticmethod
    def _to_anthropic_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
        """把内部消息转换为 Anthropic Messages API 格式。"""

        converted: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "system":
                continue
            if message.role == "tool":
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.tool_call_id or "",
                                "content": message.content,
                            }
                        ],
                    }
                )
                continue
            if message.role == "assistant" and message.tool_calls:
                content: list[dict[str, Any]] = []
                if message.content:
                    content.append({"type": "text", "text": message.content})
                for tool_call in message.tool_calls:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "input": tool_call.arguments if isinstance(tool_call.arguments, dict) else {},
                        }
                    )
                converted.append({"role": "assistant", "content": content})
                continue
            converted.append({"role": message.role, "content": message.content})
        return converted

    @staticmethod
    def _collect_text(content_blocks: list[Any]) -> str:
        """提取 Anthropic content blocks 中的文本内容。"""

        parts: list[str] = []
        for block in content_blocks:
            if _read_field(block, "type") == "text":
                parts.append(_read_field(block, "text", ""))
        return "".join(parts)

    @staticmethod
    def _parse_tool_calls(content_blocks: list[Any]) -> list[ToolCall]:
        """解析 Anthropic content blocks 中的 tool_use。"""

        parsed: list[ToolCall] = []
        for block in content_blocks:
            if _read_field(block, "type") != "tool_use":
                continue
            parsed.append(
                ToolCall(
                    id=_read_field(block, "id", ""),
                    name=_read_field(block, "name", ""),
                    arguments=_read_field(block, "input", {}) or {},
                )
            )
        return parsed


def _normalize_stop_reason(reason: Any) -> FinishReason:
    """把 Anthropic stop_reason 收敛成内部 finish_reason。

    AnthropicProvider 仍是实验性实现，但只要返回 `ChatResponse`，就必须遵守
    provider 层统一响应契约；原始 stop_reason 放到 diagnostics 里。
    """

    if reason == "end_turn" or reason == "stop_sequence":
        return "stop"
    if reason == "tool_use":
        return "tool_calls"
    if reason == "max_tokens":
        return "length"
    if reason is None:
        return "unknown"
    return "unknown"
