"""provider 层的基础行为测试。"""

from __future__ import annotations

import asyncio

import pytest

from firstcoder.providers.anthropic_provider import AnthropicProvider
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import ProviderError, ProviderErrorKind
from firstcoder.providers.openai_compatible import OpenAICompatibleProvider
from firstcoder.providers.types import (
    ChatMessage,
    ChatRequest,
    ProviderCapabilities,
    ToolCall,
    ToolChoiceFunction,
    ToolDefinition,
)


class _Object:
    """用于模拟 SDK 返回对象的轻量测试对象。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeOpenAICompletions:
    def __init__(self):
        self.last_params = None

    def create(self, **params):
        self.last_params = params
        return _Object(
            model=params["model"],
            usage=_Object(prompt_tokens=11, completion_tokens=7, total_tokens=18),
            choices=[
                _Object(
                    finish_reason="tool_calls",
                    message=_Object(
                        content="",
                        tool_calls=[
                            _Object(
                                id="call_1",
                                function=_Object(name="read_file", arguments='{"path": "README.md"}'),
                            )
                        ],
                    ),
                )
            ],
        )


class _FakeOpenAIClient:
    def __init__(self):
        self.completions = _FakeOpenAICompletions()
        self.chat = _Object(completions=self.completions)


class _FakeOpenAILengthCompletions:
    def __init__(self):
        self.last_params = None

    def create(self, **params):
        self.last_params = params
        return _Object(
            model=params["model"],
            choices=[
                _Object(
                    finish_reason="length",
                    message=_Object(
                        content="",
                        tool_calls=[
                            _Object(
                                id="call_partial",
                                function=_Object(name="read_file", arguments='{"path": "README'),
                            )
                        ],
                    ),
                )
            ],
        )


class _FakeOpenAILengthClient:
    def __init__(self):
        self.completions = _FakeOpenAILengthCompletions()
        self.chat = _Object(completions=self.completions)


class _FakeOpenAIInvalidArgumentsCompletions:
    def create(self, **params):
        return _Object(
            model=params["model"],
            choices=[
                _Object(
                    finish_reason="tool_calls",
                    message=_Object(
                        content="",
                        tool_calls=[
                            _Object(
                                id="call_bad_json",
                                function=_Object(name="read_file", arguments='{"path": "README.md"'),
                            )
                        ],
                    ),
                )
            ],
        )


class _FakeOpenAIInvalidArgumentsClient:
    def __init__(self):
        self.chat = _Object(completions=_FakeOpenAIInvalidArgumentsCompletions())


class _FakeOpenAIMixedArgumentsCompletions:
    def create(self, **params):
        return _Object(
            model=params["model"],
            choices=[
                _Object(
                    finish_reason="tool_calls",
                    message=_Object(
                        content="",
                        tool_calls=[
                            _Object(
                                id="call_good",
                                function=_Object(name="grep", arguments='{"pattern": "TODO"}'),
                            ),
                            _Object(
                                id="call_bad",
                                function=_Object(name="read_file", arguments='{"path": "README.md"'),
                            ),
                        ],
                    ),
                )
            ],
        )


class _FakeOpenAIMixedArgumentsClient:
    def __init__(self):
        self.chat = _Object(completions=_FakeOpenAIMixedArgumentsCompletions())


class _StatusError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class _FakeOpenAIErrorCompletions:
    def create(self, **params):
        raise _StatusError("upstream unavailable", 503)


class _FakeOpenAIErrorClient:
    def __init__(self):
        self.chat = _Object(completions=_FakeOpenAIErrorCompletions())


class _ResponseStatusError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.response = _Object(status_code=status_code)


class _FakeOpenAIResponseErrorCompletions:
    def create(self, **params):
        raise _ResponseStatusError("too many requests", 429)


class _FakeOpenAIResponseErrorClient:
    def __init__(self):
        self.chat = _Object(completions=_FakeOpenAIResponseErrorCompletions())


class _FakeAnthropicMessages:
    def __init__(self):
        self.last_params = None

    def create(self, **params):
        self.last_params = params
        return _Object(
            model=params["model"],
            stop_reason="tool_use",
            content=[
                _Object(type="text", text="我需要读取文件。"),
                _Object(type="tool_use", id="toolu_1", name="read_file", input={"path": "README.md"}),
            ],
        )


class _FakeAnthropicClient:
    def __init__(self):
        self.messages = _FakeAnthropicMessages()


class _NoStreamProvider(ChatProvider):
    @property
    def name(self) -> str:
        return "no-stream"

    @property
    def model(self) -> str:
        return "test-model"

    def complete(self, request: ChatRequest):
        raise AssertionError("not used")


def test_openai_compatible_provider_parses_tool_calls():
    client = _FakeOpenAIClient()
    provider = OpenAICompatibleProvider(
        name="test-openai",
        model="test-model",
        api_key="test-key",
        client=client,
    )

    response = provider.complete(
        ChatRequest(
            messages=[ChatMessage(role="user", content="读取 README")],
            tools=[
                ToolDefinition(
                    name="read_file",
                    description="读取文件",
                    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
                )
            ],
        )
    )

    assert client.completions.last_params["tools"][0]["function"]["name"] == "read_file"
    assert response.provider == "test-openai"
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "README.md"}
    assert response.finish_reason == "tool_calls"
    assert response.diagnostics.raw_finish_reason == "tool_calls"
    assert response.usage is not None
    assert response.usage.input_tokens == 11
    assert response.usage.output_tokens == 7
    assert response.usage.total_tokens == 18


def test_openai_compatible_provider_serializes_assistant_tool_calls():
    client = _FakeOpenAIClient()
    provider = OpenAICompatibleProvider(
        name="test-openai",
        model="test-model",
        api_key="test-key",
        client=client,
    )

    provider.complete(
        ChatRequest(
            messages=[
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(id="call_1", name="read_file", arguments={"path": "README.md"}),
                    ],
                )
            ],
        )
    )

    sent_message = client.completions.last_params["messages"][0]
    assert sent_message["tool_calls"][0]["function"]["arguments"] == '{"path":"README.md"}'


def test_openai_compatible_provider_uses_capability_token_param_and_extra_body():
    client = _FakeOpenAIClient()
    provider = OpenAICompatibleProvider(
        name="test-openai",
        model="test-model",
        api_key="test-key",
        client=client,
        capabilities=ProviderCapabilities(token_param="max_completion_tokens"),
        extra_body={"preset": True},
    )

    provider.complete(
        ChatRequest(
            messages=[ChatMessage(role="user", content="hi")],
            max_tokens=123,
            extra_body={"request": True},
        )
    )

    assert "max_tokens" not in client.completions.last_params
    assert client.completions.last_params["max_completion_tokens"] == 123
    assert client.completions.last_params["extra_body"] == {"preset": True, "request": True}


def test_openai_compatible_provider_converts_forced_tool_choice():
    client = _FakeOpenAIClient()
    provider = OpenAICompatibleProvider(
        name="test-openai",
        model="test-model",
        api_key="test-key",
        client=client,
    )

    provider.complete(
        ChatRequest(
            messages=[ChatMessage(role="user", content="读取 README")],
            tools=[ToolDefinition(name="read_file", description="读取文件")],
            tool_choice=ToolChoiceFunction(name="read_file"),
        )
    )

    assert client.completions.last_params["tool_choice"] == {
        "type": "function",
        "function": {"name": "read_file"},
    }


def test_openai_compatible_provider_rejects_raw_dict_tool_choice_with_provider_error():
    provider = OpenAICompatibleProvider(
        name="test-openai",
        model="test-model",
        api_key="test-key",
        client=_FakeOpenAIClient(),
    )

    with pytest.raises(ProviderError) as exc_info:
        provider.complete(
            ChatRequest(
                messages=[ChatMessage(role="user", content="读取 README")],
                tools=[ToolDefinition(name="read_file", description="读取文件")],
                tool_choice={"type": "function", "function": {"name": "read_file"}},  # type: ignore[arg-type]
            )
        )

    assert exc_info.value.kind == ProviderErrorKind.CONFIG_ERROR


def test_openai_compatible_provider_rejects_tools_when_capability_disabled():
    provider = OpenAICompatibleProvider(
        name="no-tools",
        model="test-model",
        api_key="test-key",
        client=_FakeOpenAIClient(),
        capabilities=ProviderCapabilities(supports_tools=False),
    )

    with pytest.raises(ProviderError) as exc_info:
        provider.complete(
            ChatRequest(
                messages=[ChatMessage(role="user", content="读取 README")],
                tools=[ToolDefinition(name="read_file", description="读取文件")],
            )
        )

    assert exc_info.value.kind == ProviderErrorKind.CONFIG_ERROR
    assert exc_info.value.retryable is False


def test_openai_compatible_provider_drops_tool_calls_when_response_is_truncated():
    provider = OpenAICompatibleProvider(
        name="test-openai",
        model="test-model",
        api_key="test-key",
        client=_FakeOpenAILengthClient(),
    )

    response = provider.complete(ChatRequest(messages=[ChatMessage(role="user", content="读取 README")]))

    assert response.finish_reason == "length"
    assert response.tool_calls == []
    assert response.diagnostics.warnings


def test_openai_compatible_provider_drops_tool_calls_with_invalid_json_arguments():
    provider = OpenAICompatibleProvider(
        name="test-openai",
        model="test-model",
        api_key="test-key",
        client=_FakeOpenAIInvalidArgumentsClient(),
    )

    response = provider.complete(ChatRequest(messages=[ChatMessage(role="user", content="读取 README")]))

    assert response.finish_reason == "tool_calls"
    assert response.tool_calls == []
    assert response.diagnostics.warnings


def test_openai_compatible_provider_drops_all_tool_calls_when_any_arguments_are_invalid():
    provider = OpenAICompatibleProvider(
        name="test-openai",
        model="test-model",
        api_key="test-key",
        client=_FakeOpenAIMixedArgumentsClient(),
    )

    response = provider.complete(ChatRequest(messages=[ChatMessage(role="user", content="查找 TODO")]))

    assert response.finish_reason == "tool_calls"
    assert response.tool_calls == []
    assert response.diagnostics.warnings


def test_openai_compatible_provider_wraps_status_error_kind():
    provider = OpenAICompatibleProvider(
        name="test-openai",
        model="test-model",
        api_key="test-key",
        client=_FakeOpenAIErrorClient(),
    )

    with pytest.raises(ProviderError) as exc_info:
        provider.complete(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))

    assert exc_info.value.kind == ProviderErrorKind.SERVER_ERROR
    assert exc_info.value.retryable is True


def test_openai_compatible_provider_wraps_response_status_error_kind():
    provider = OpenAICompatibleProvider(
        name="test-openai",
        model="test-model",
        api_key="test-key",
        client=_FakeOpenAIResponseErrorClient(),
    )

    with pytest.raises(ProviderError) as exc_info:
        provider.complete(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))

    assert exc_info.value.kind == ProviderErrorKind.RATE_LIMIT
    assert exc_info.value.retryable is True


def test_chat_provider_default_astream_reports_unsupported():
    async def collect_stream_error() -> None:
        events = _NoStreamProvider().astream(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
        with pytest.raises(ProviderError) as exc_info:
            async for _event in events:
                pass
        assert exc_info.value.kind == ProviderErrorKind.UNSUPPORTED

    asyncio.run(collect_stream_error())


def test_anthropic_provider_parses_text_and_tool_calls():
    client = _FakeAnthropicClient()
    provider = AnthropicProvider(model="claude-test", api_key="test-key", client=client)

    response = provider.complete(
        ChatRequest(
            messages=[
                ChatMessage(role="system", content="你是 coding agent"),
                ChatMessage(role="user", content="读取 README"),
            ],
            tools=[
                ToolDefinition(
                    name="read_file",
                    description="读取文件",
                    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
                )
            ],
        )
    )

    assert client.messages.last_params["system"] == "你是 coding agent"
    assert client.messages.last_params["tools"][0]["name"] == "read_file"
    assert response.content == "我需要读取文件。"
    assert response.tool_calls[0].arguments == {"path": "README.md"}
    assert response.finish_reason == "tool_calls"
    assert response.diagnostics.raw_finish_reason == "tool_use"


def test_anthropic_provider_rejects_non_auto_tool_choice():
    provider = AnthropicProvider(model="claude-test", api_key="test-key", client=_FakeAnthropicClient())

    with pytest.raises(ProviderError) as exc_info:
        provider.complete(
            ChatRequest(
                messages=[ChatMessage(role="user", content="读取 README")],
                tools=[ToolDefinition(name="read_file", description="读取文件")],
                tool_choice=ToolChoiceFunction(name="read_file"),
            )
        )

    assert exc_info.value.kind == ProviderErrorKind.CONFIG_ERROR


def test_anthropic_provider_serializes_assistant_tool_calls():
    client = _FakeAnthropicClient()
    provider = AnthropicProvider(model="claude-test", api_key="test-key", client=client)

    provider.complete(
        ChatRequest(
            messages=[
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(id="toolu_1", name="read_file", arguments={"path": "README.md"}),
                    ],
                )
            ],
        )
    )

    sent_message = client.messages.last_params["messages"][0]
    assert sent_message["content"][0]["type"] == "tool_use"
    assert sent_message["content"][0]["input"] == {"path": "README.md"}
