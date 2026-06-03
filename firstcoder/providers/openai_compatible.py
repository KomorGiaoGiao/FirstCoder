"""OpenAI-compatible provider 实现。"""

from __future__ import annotations

from typing import Any

from firstcoder.utils.json_utils import dumps_json, loads_json_object
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import ProviderError, ProviderErrorKind, classify_provider_error
from firstcoder.providers.tool_adapters import to_openai_tool
from firstcoder.providers.types import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    FinishReason,
    ProviderCapabilities,
    ProviderDiagnostics,
    TokenUsage,
    ToolChoice,
    ToolChoiceFunction,
    ToolCall,
)


def _read_field(value: Any, name: str, default: Any = None) -> Any:
    """同时兼容 SDK 对象和普通 dict 的字段读取。

    OpenAI SDK 返回的是带属性访问能力的对象；测试里常常用 dict 或简单假对象。
    统一通过这个函数读取字段，可以让解析逻辑更容易测试。
    """

    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class OpenAICompatibleProvider(ChatProvider):
    """使用 OpenAI Chat Completions 协议的 provider。

    OpenAI、DeepSeek、Qwen、Moonshot、Zhipu、OpenRouter、Ollama 等都可以通过
    `base_url + api_key + model` 的方式接入这一层。不同厂商的高级参数可以通过
    `ChatRequest.extra_body` 继续透传。
    """

    def __init__(
        self,
        *,
        name: str,
        model: str,
        api_key: str,
        base_url: str | None = None,
        capabilities: ProviderCapabilities | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        client: Any | None = None,
    ) -> None:
        self._name = name
        self._model = model
        self._base_url = base_url
        self._capabilities = capabilities or ProviderCapabilities()
        self._extra_headers = dict(extra_headers or {})
        self._extra_body = dict(extra_body or {})

        # 允许测试或上层代码注入 client；没有注入时才创建真实 SDK client。
        if client is not None:
            self._client = client
        else:
            from openai import OpenAI

            kwargs: dict[str, Any] = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            if extra_headers:
                kwargs["default_headers"] = extra_headers
            self._client = OpenAI(**kwargs)

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    @property
    def base_url(self) -> str | None:
        return self._base_url

    @property
    def extra_headers(self) -> dict[str, str]:
        return dict(self._extra_headers)

    @property
    def extra_body(self) -> dict[str, Any]:
        return dict(self._extra_body)

    def complete(self, request: ChatRequest) -> ChatResponse:
        """调用 Chat Completions，并转换成项目内部统一响应。"""

        if request.tools and not self._capabilities.supports_tools:
            raise ProviderError(
                ProviderErrorKind.CONFIG_ERROR,
                f"provider {self._name} 不支持 tool calling，不能发送 tools",
            )

        params: dict[str, Any] = {
            "model": self._model,
            "messages": [self._to_openai_message(message) for message in request.messages],
        }

        if request.tools:
            params["tools"] = [to_openai_tool(tool) for tool in request.tools]
            params["tool_choice"] = _to_openai_tool_choice(request.tool_choice)
            if self._capabilities.supports_parallel_tool_calls:
                params["parallel_tool_calls"] = True
        if request.temperature is not None:
            params["temperature"] = request.temperature
        if request.max_tokens is not None:
            params[self._capabilities.token_param] = request.max_tokens

        extra_body = {**self._extra_body, **request.extra_body}
        if extra_body:
            params["extra_body"] = extra_body

        try:
            response = self._client.chat.completions.create(**params)
        except Exception as exc:
            message = str(exc)
            raise ProviderError(classify_provider_error(message), message) from exc
        choice = _read_field(response, "choices", [])[0]
        message = _read_field(choice, "message")
        raw_finish_reason = _read_field(choice, "finish_reason")
        finish_reason = _normalize_finish_reason(raw_finish_reason)
        diagnostics = ProviderDiagnostics(raw_finish_reason=raw_finish_reason)
        tool_calls = self._parse_tool_calls(_read_field(message, "tool_calls", []) or [], diagnostics=diagnostics)
        if finish_reason == "length" and tool_calls:
            diagnostics.warnings.append("finish_reason=length，丢弃可能不完整的 tool_calls，避免执行半截工具调用。")
            tool_calls = []

        return ChatResponse(
            provider=self._name,
            model=_read_field(response, "model", self._model),
            content=_read_field(message, "content", "") or "",
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=_parse_usage(_read_field(response, "usage")),
            diagnostics=diagnostics,
            raw=response,
        )

    @staticmethod
    def _to_openai_message(message: ChatMessage) -> dict[str, Any]:
        """把内部消息转换为 OpenAI-compatible 消息。"""

        data: dict[str, Any] = {
            "role": message.role,
            "content": message.content,
        }
        if message.tool_calls:
            data["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.name,
                        "arguments": (
                            tool_call.arguments
                            if isinstance(tool_call.arguments, str)
                            else dumps_json(tool_call.arguments)
                        ),
                    },
                }
                for tool_call in message.tool_calls
            ]
        if message.name:
            data["name"] = message.name
        if message.tool_call_id:
            data["tool_call_id"] = message.tool_call_id
        return data

    @staticmethod
    def _parse_tool_calls(tool_calls: list[Any], *, diagnostics: ProviderDiagnostics) -> list[ToolCall]:
        """解析 OpenAI-compatible 返回的 tool_calls。"""

        parsed: list[ToolCall] = []
        for call in tool_calls:
            function = _read_field(call, "function", {})
            raw_arguments = _read_field(function, "arguments", "")
            arguments = loads_json_object(raw_arguments)
            if not isinstance(arguments, dict):
                call_id = _read_field(call, "id", "")
                name = _read_field(function, "name", "")
                diagnostics.warnings.append(
                    f"tool_call 参数不是合法 JSON object，已丢弃整组不可执行调用：id={call_id}, name={name}"
                )
                return []

            parsed.append(
                ToolCall(
                    id=_read_field(call, "id", ""),
                    name=_read_field(function, "name", ""),
                    arguments=arguments,
                )
            )
        return parsed


def _normalize_finish_reason(reason: Any) -> FinishReason:
    """把 OpenAI-compatible finish_reason 收敛成内部受控值。"""

    if reason in {"stop", "tool_calls", "length", "content_filter"}:
        return reason
    if reason is None:
        return "unknown"
    return "unknown"


def _parse_usage(usage: Any) -> TokenUsage | None:
    """解析 OpenAI-compatible usage 字段，缺字段时保留 None。"""

    if usage is None:
        return None
    input_tokens = _read_field(usage, "prompt_tokens")
    output_tokens = _read_field(usage, "completion_tokens")
    total_tokens = _read_field(usage, "total_tokens")
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _to_openai_tool_choice(tool_choice: ToolChoice | None) -> str | dict[str, Any] | None:
    """把内部 tool_choice 转成 OpenAI function calling wire format。"""

    if tool_choice is None:
        return None
    if isinstance(tool_choice, ToolChoiceFunction):
        return {
            "type": "function",
            "function": {"name": tool_choice.name},
        }
    if isinstance(tool_choice, str) and tool_choice in {"auto", "none", "required"}:
        return tool_choice
    raise ProviderError(ProviderErrorKind.CONFIG_ERROR, f"不支持的 tool_choice：{tool_choice!r}")
