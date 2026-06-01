"""OpenAI-compatible provider 实现。"""

from __future__ import annotations

from typing import Any

from firstcoder.utils.json_utils import dumps_json, loads_json_object
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.tool_adapters import to_openai_tool
from firstcoder.providers.types import ChatMessage, ChatRequest, ChatResponse, ToolCall


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
        client: Any | None = None,
    ) -> None:
        self._name = name
        self._model = model

        # 允许测试或上层代码注入 client；没有注入时才创建真实 SDK client。
        if client is not None:
            self._client = client
        else:
            from openai import OpenAI

            kwargs: dict[str, Any] = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            self._client = OpenAI(**kwargs)

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model

    def complete(self, request: ChatRequest) -> ChatResponse:
        """调用 Chat Completions，并转换成项目内部统一响应。"""

        params: dict[str, Any] = {
            "model": self._model,
            "messages": [self._to_openai_message(message) for message in request.messages],
        }

        if request.tools:
            params["tools"] = [to_openai_tool(tool) for tool in request.tools]
            params["tool_choice"] = request.tool_choice
        if request.temperature is not None:
            params["temperature"] = request.temperature
        if request.max_tokens is not None:
            params["max_tokens"] = request.max_tokens
        if request.extra_body:
            params["extra_body"] = request.extra_body

        response = self._client.chat.completions.create(**params)
        choice = _read_field(response, "choices", [])[0]
        message = _read_field(choice, "message")

        return ChatResponse(
            provider=self._name,
            model=_read_field(response, "model", self._model),
            content=_read_field(message, "content", "") or "",
            tool_calls=self._parse_tool_calls(_read_field(message, "tool_calls", []) or []),
            finish_reason=_read_field(choice, "finish_reason"),
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
    def _parse_tool_calls(tool_calls: list[Any]) -> list[ToolCall]:
        """解析 OpenAI-compatible 返回的 tool_calls。"""

        parsed: list[ToolCall] = []
        for call in tool_calls:
            function = _read_field(call, "function", {})
            raw_arguments = _read_field(function, "arguments", "")

            parsed.append(
                ToolCall(
                    id=_read_field(call, "id", ""),
                    name=_read_field(function, "name", ""),
                    arguments=loads_json_object(raw_arguments),
                )
            )
        return parsed
