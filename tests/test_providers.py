"""provider 层的基础行为测试。"""

from __future__ import annotations

import asyncio
import threading

import pytest

from firstcoder.providers.anthropic_provider import AnthropicProvider
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import ProviderError, ProviderErrorKind
from firstcoder.providers.openai_compatible import OpenAICompatibleProvider
from firstcoder.providers.types import (
    ChatMessage,
    ChatRequest,
    ProviderCapabilities,
    TokenUsage,
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


class _FakeOpenAITextStreamCompletions:
    def __init__(self):
        self.last_params = None

    def create(self, **params):
        self.last_params = params
        return iter(
            [
                _Object(
                    model=params["model"],
                    choices=[_Object(delta=_Object(content="你"), finish_reason=None)],
                ),
                _Object(
                    model=params["model"],
                    choices=[_Object(delta=_Object(content="好"), finish_reason=None)],
                ),
                _Object(
                    model=params["model"],
                    choices=[_Object(delta=_Object(), finish_reason="stop")],
                ),
            ]
        )


class _FakeOpenAITextStreamClient:
    def __init__(self):
        self.completions = _FakeOpenAITextStreamCompletions()
        self.chat = _Object(completions=self.completions)


class _FakeOpenAIReasoningStreamCompletions:
    def create(self, **params):
        return iter(
            [
                _Object(
                    model=params["model"],
                    choices=[_Object(delta=_Object(reasoning_content="先想"), finish_reason=None)],
                ),
                _Object(
                    model=params["model"],
                    choices=[_Object(delta={"reasoning": {"content": "一下"}}, finish_reason=None)],
                ),
                _Object(
                    model=params["model"],
                    choices=[_Object(delta=_Object(content="答"), finish_reason=None)],
                ),
                _Object(
                    model=params["model"],
                    choices=[_Object(delta=_Object(), finish_reason="stop")],
                ),
            ]
        )


class _FakeOpenAIReasoningStreamClient:
    def __init__(self):
        self.chat = _Object(completions=_FakeOpenAIReasoningStreamCompletions())


class _FakeOpenAIToolStreamCompletions:
    def __init__(self):
        self.last_params = None

    def create(self, **params):
        self.last_params = params
        return iter(
            [
                _Object(
                    model=params["model"],
                    choices=[
                        _Object(
                            delta=_Object(
                                tool_calls=[
                                    _Object(
                                        index=0,
                                        id="call_1",
                                        function=_Object(name="read_file", arguments='{"path"'),
                                    )
                                ]
                            ),
                            finish_reason=None,
                        )
                    ],
                ),
                _Object(
                    model=params["model"],
                    choices=[
                        _Object(
                            delta=_Object(
                                tool_calls=[
                                    _Object(
                                        index=0,
                                        function=_Object(arguments=': "README.md"}'),
                                    )
                                ]
                            ),
                            finish_reason=None,
                        )
                    ],
                ),
                _Object(
                    model=params["model"],
                    choices=[_Object(delta=_Object(), finish_reason="tool_calls")],
                ),
            ]
        )


class _FakeOpenAIToolStreamClient:
    def __init__(self):
        self.completions = _FakeOpenAIToolStreamCompletions()
        self.chat = _Object(completions=self.completions)


class _FakeOpenAITruncatedToolStreamCompletions:
    def create(self, **params):
        return iter(
            [
                _Object(
                    model=params["model"],
                    choices=[
                        _Object(
                            delta=_Object(
                                tool_calls=[
                                    _Object(
                                        index=0,
                                        id="call_partial",
                                        function=_Object(name="read_file", arguments='{"path": "README'),
                                    )
                                ]
                            ),
                            finish_reason=None,
                        )
                    ],
                ),
                _Object(
                    model=params["model"],
                    choices=[_Object(delta=_Object(), finish_reason="length")],
                ),
            ]
        )


class _FakeOpenAITruncatedToolStreamClient:
    def __init__(self):
        self.chat = _Object(completions=_FakeOpenAITruncatedToolStreamCompletions())


class _FakeOpenAIMultiToolStreamCompletions:
    def create(self, **params):
        return iter(
            [
                _Object(
                    model=params["model"],
                    choices=[
                        _Object(
                            delta=_Object(
                                tool_calls=[
                                    _Object(
                                        index=1,
                                        id="call_b",
                                        function=_Object(name="grep", arguments='{"pattern"'),
                                    ),
                                    _Object(
                                        index=0,
                                        id="call_a",
                                        function=_Object(name="read_file", arguments='{"path"'),
                                    ),
                                ]
                            ),
                            finish_reason=None,
                        )
                    ],
                ),
                _Object(
                    model=params["model"],
                    choices=[
                        _Object(
                            delta=_Object(
                                tool_calls=[
                                    _Object(index=0, function=_Object(arguments=': "README.md"}')),
                                    _Object(index=1, function=_Object(arguments=': "TODO"}')),
                                ]
                            ),
                            finish_reason=None,
                        )
                    ],
                ),
                _Object(
                    model=params["model"],
                    choices=[_Object(delta=_Object(), finish_reason="tool_calls")],
                ),
            ]
        )


class _FakeOpenAIMultiToolStreamClient:
    def __init__(self):
        self.chat = _Object(completions=_FakeOpenAIMultiToolStreamCompletions())


class _FakeOpenAIInvalidToolStreamCompletions:
    def create(self, **params):
        return iter(
            [
                _Object(
                    model=params["model"],
                    choices=[
                        _Object(
                            delta=_Object(
                                tool_calls=[
                                    _Object(
                                        index=0,
                                        id="call_bad",
                                        function=_Object(name="read_file", arguments='{"path": "README.md"'),
                                    )
                                ]
                            ),
                            finish_reason=None,
                        )
                    ],
                ),
                _Object(
                    model=params["model"],
                    choices=[_Object(delta=_Object(), finish_reason="tool_calls")],
                ),
            ]
        )


class _FakeOpenAIInvalidToolStreamClient:
    def __init__(self):
        self.chat = _Object(completions=_FakeOpenAIInvalidToolStreamCompletions())


class _FakeOpenAIMissingArgumentsToolStreamCompletions:
    def create(self, **params):
        return iter(
            [
                _Object(
                    model=params["model"],
                    choices=[
                        _Object(
                            delta=_Object(
                                tool_calls=[
                                    _Object(index=0, id="call_empty", function=_Object(name="read_file"))
                                ]
                            ),
                            finish_reason=None,
                        )
                    ],
                ),
                _Object(
                    model=params["model"],
                    choices=[_Object(delta=_Object(), finish_reason="tool_calls")],
                ),
            ]
        )


class _FakeOpenAIMissingArgumentsToolStreamClient:
    def __init__(self):
        self.chat = _Object(completions=_FakeOpenAIMissingArgumentsToolStreamCompletions())


class _FakeOpenAIStopToolStreamCompletions:
    def create(self, **params):
        return iter(
            [
                _Object(
                    model=params["model"],
                    choices=[
                        _Object(
                            delta=_Object(
                                tool_calls=[
                                    _Object(
                                        index=0,
                                        id="call_stop",
                                        function=_Object(name="read_file", arguments='{"path": "README.md"}'),
                                    )
                                ]
                            ),
                            finish_reason=None,
                        )
                    ],
                ),
                _Object(
                    model=params["model"],
                    choices=[_Object(delta=_Object(), finish_reason="stop")],
                ),
            ]
        )


class _FakeOpenAIStopToolStreamClient:
    def __init__(self):
        self.chat = _Object(completions=_FakeOpenAIStopToolStreamCompletions())


class _FakeOpenAIErrorChunkStreamCompletions:
    def create(self, **params):
        return iter([_Object(error=_Object(message="rate limit exceeded", status_code=429))])


class _FakeOpenAIErrorChunkStreamClient:
    def __init__(self):
        self.chat = _Object(completions=_FakeOpenAIErrorChunkStreamCompletions())


class _FailingStream:
    def __iter__(self):
        return self

    def __next__(self):
        raise _StatusError("stream unavailable", 503)


class _FakeOpenAIFailingStreamCompletions:
    def create(self, **params):
        return _FailingStream()


class _FakeOpenAIFailingStreamClient:
    def __init__(self):
        self.chat = _Object(completions=_FakeOpenAIFailingStreamCompletions())


class _FakeAnthropicMessages:
    def __init__(self):
        self.last_params = None

    def create(self, **params):
        self.last_params = params
        if params.get("stream"):
            return self._stream_events(params)
        return _Object(
            model=params["model"],
            stop_reason="tool_use",
            usage=_Object(input_tokens=12, output_tokens=8),
            content=[
                _Object(type="text", text="我需要读取文件。"),
                _Object(type="tool_use", id="toolu_1", name="read_file", input={"path": "README.md"}),
            ],
        )

    def _stream_events(self, params):
        return iter(
            [
                _Object(
                    type="message_start",
                    message=_Object(model=params["model"], usage=_Object(input_tokens=12, output_tokens=0)),
                ),
                _Object(
                    type="content_block_start",
                    index=0,
                    content_block=_Object(type="text", text=""),
                ),
                _Object(
                    type="content_block_delta",
                    index=0,
                    delta=_Object(type="text_delta", text="我需要"),
                ),
                _Object(
                    type="content_block_delta",
                    index=0,
                    delta=_Object(type="text_delta", text="读取文件。"),
                ),
                _Object(type="content_block_stop", index=0),
                _Object(
                    type="content_block_start",
                    index=1,
                    content_block=_Object(type="tool_use", id="toolu_1", name="read_file", input={}),
                ),
                _Object(
                    type="content_block_delta",
                    index=1,
                    delta=_Object(type="input_json_delta", partial_json='{"path"'),
                ),
                _Object(
                    type="content_block_delta",
                    index=1,
                    delta=_Object(type="input_json_delta", partial_json=': "README.md"}'),
                ),
                _Object(type="content_block_stop", index=1),
                _Object(
                    type="message_delta",
                    delta=_Object(stop_reason="tool_use"),
                    usage=_Object(output_tokens=8),
                ),
                _Object(type="message_stop"),
            ]
        )


class _FakeAnthropicClient:
    def __init__(self):
        self.messages = _FakeAnthropicMessages()


class _FakeAnthropicTextStreamMessages:
    def __init__(self):
        self.last_params = None

    def create(self, **params):
        self.last_params = params
        assert params.get("stream") is True
        return iter(
            [
                _Object(type="message_start", message=_Object(model=params["model"], usage=_Object(input_tokens=3))),
                _Object(type="content_block_start", index=0, content_block=_Object(type="text", text="")),
                _Object(type="content_block_delta", index=0, delta=_Object(type="text_delta", text="你")),
                _Object(type="content_block_delta", index=0, delta=_Object(type="text_delta", text="好")),
                _Object(type="content_block_stop", index=0),
                _Object(type="message_delta", delta=_Object(stop_reason="end_turn"), usage=_Object(output_tokens=2)),
                _Object(type="message_stop"),
            ]
        )


class _FakeAnthropicTextStreamClient:
    def __init__(self):
        self.messages = _FakeAnthropicTextStreamMessages()


class _ClosableAnthropicStream:
    def __init__(self):
        self.close_count = 0
        self._closed = threading.Event()
        self._yielded = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self._yielded:
            self._yielded = True
            return _Object(type="content_block_delta", index=0, delta=_Object(type="text_delta", text="x"))
        self._closed.wait()
        raise StopIteration

    def close(self):
        self.close_count += 1
        self._closed.set()


class _ClosableAnthropicStreamMessages:
    def __init__(self):
        self.stream = _ClosableAnthropicStream()

    def create(self, **params):
        assert params.get("stream") is True
        return self.stream


class _ClosableAnthropicStreamClient:
    def __init__(self):
        self.messages = _ClosableAnthropicStreamMessages()


class _FakeAnthropicThinkingStreamMessages:
    def create(self, **params):
        return iter(
            [
                _Object(type="message_start", message=_Object(model=params["model"])),
                _Object(
                    type="content_block_delta",
                    index=0,
                    delta=_Object(type="thinking_delta", thinking="先想"),
                ),
                _Object(
                    type="content_block_delta",
                    index=0,
                    delta=_Object(type="thinking_delta", thinking="一下"),
                ),
                _Object(
                    type="content_block_delta",
                    index=1,
                    delta=_Object(type="text_delta", text="答"),
                ),
                _Object(type="message_delta", delta=_Object(stop_reason="end_turn")),
                _Object(type="message_stop"),
            ]
        )


class _FakeAnthropicThinkingStreamClient:
    def __init__(self):
        self.messages = _FakeAnthropicThinkingStreamMessages()


class _FakeAnthropicTruncatedToolStreamMessages:
    def create(self, **params):
        return iter(
            [
                _Object(type="message_start", message=_Object(model=params["model"])),
                _Object(
                    type="content_block_start",
                    index=0,
                    content_block=_Object(type="tool_use", id="toolu_1", name="read_file", input={}),
                ),
                _Object(
                    type="content_block_delta",
                    index=0,
                    delta=_Object(type="input_json_delta", partial_json='{"path":'),
                ),
                _Object(type="message_delta", delta=_Object(stop_reason="max_tokens")),
                _Object(type="message_stop"),
            ]
        )


class _FakeAnthropicTruncatedToolStreamClient:
    def __init__(self):
        self.messages = _FakeAnthropicTruncatedToolStreamMessages()


class _FakeAnthropicLengthMessages:
    def create(self, **params):
        return _Object(
            model=params["model"],
            stop_reason="max_tokens",
            usage=_Object(input_tokens=5, output_tokens=9),
            content=[
                _Object(type="tool_use", id="toolu_1", name="read_file", input={"path": "README.md"}),
            ],
        )


class _FakeAnthropicLengthClient:
    def __init__(self):
        self.messages = _FakeAnthropicLengthMessages()


class _NoStreamProvider(ChatProvider):
    @property
    def name(self) -> str:
        return "no-stream"

    @property
    def model(self) -> str:
        return "test-model"

    def complete(self, request: ChatRequest):
        raise AssertionError("not used")


@pytest.mark.parametrize("provider_kind", ["openai", "anthropic"])
def test_provider_usage_normalizes_to_shared_token_usage(provider_kind) -> None:
    if provider_kind == "openai":
        provider = OpenAICompatibleProvider(
            name="test-openai",
            model="test-model",
            api_key="test-key",
            client=_FakeOpenAIClient(),
        )
        expected = TokenUsage(input_tokens=11, output_tokens=7, total_tokens=18)
    else:
        provider = AnthropicProvider(
            model="claude-test",
            api_key="test-key",
            client=_FakeAnthropicLengthClient(),
        )
        expected = TokenUsage(input_tokens=5, output_tokens=9, total_tokens=14)

    response = provider.complete(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))

    assert response.usage == expected


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
            extra_body={"request": True, "reasoning_effort": "high"},
        )
    )

    assert "max_tokens" not in client.completions.last_params
    assert client.completions.last_params["max_completion_tokens"] == 123
    assert client.completions.last_params["extra_body"] == {
        "preset": True,
        "request": True,
        "reasoning_effort": "high",
    }


def test_openai_compatible_provider_sends_parallel_tool_calls_when_supported():
    client = _FakeOpenAIClient()
    provider = OpenAICompatibleProvider(
        name="test-openai",
        model="test-model",
        api_key="test-key",
        client=client,
        capabilities=ProviderCapabilities(supports_parallel_tool_calls=True),
    )

    provider.complete(
        ChatRequest(
            messages=[ChatMessage(role="user", content="读取文件")],
            tools=[ToolDefinition(name="read_file", description="读取文件")],
        )
    )

    assert client.completions.last_params["parallel_tool_calls"] is True


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


def test_openai_compatible_provider_streams_text_deltas_and_final_response():
    async def collect_events():
        client = _FakeOpenAITextStreamClient()
        provider = OpenAICompatibleProvider(
            name="test-openai",
            model="test-model",
            api_key="test-key",
            client=client,
        )

        events = [
            event
            async for event in provider.astream(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
        ]
        return client, events

    client, events = asyncio.run(collect_events())

    assert client.completions.last_params["stream"] is True
    assert [event.kind for event in events] == [
        "message_started",
        "text_delta",
        "text_delta",
        "message_completed",
    ]
    assert [event.text for event in events if event.kind == "text_delta"] == ["你", "好"]
    assert events[-1].response is not None
    assert events[-1].response.content == "你好"
    assert events[-1].response.finish_reason == "stop"


def test_openai_compatible_provider_streams_reasoning_deltas_into_diagnostics():
    async def collect_events():
        provider = OpenAICompatibleProvider(
            name="test-openai",
            model="test-model",
            api_key="test-key",
            client=_FakeOpenAIReasoningStreamClient(),
        )

        return [
            event
            async for event in provider.astream(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
        ]

    events = asyncio.run(collect_events())

    assert [event.kind for event in events] == [
        "message_started",
        "reasoning_delta",
        "reasoning_delta",
        "text_delta",
        "message_completed",
    ]
    assert [event.text for event in events if event.kind == "reasoning_delta"] == ["先想", "一下"]
    assert events[-1].response is not None
    assert events[-1].response.content == "答"
    assert events[-1].response.diagnostics.reasoning == "先想一下"


def test_openai_compatible_provider_accumulates_streaming_tool_calls():
    async def collect_events():
        client = _FakeOpenAIToolStreamClient()
        provider = OpenAICompatibleProvider(
            name="test-openai",
            model="test-model",
            api_key="test-key",
            client=client,
        )

        events = [
            event
            async for event in provider.astream(
                ChatRequest(
                    messages=[ChatMessage(role="user", content="读取 README")],
                    tools=[ToolDefinition(name="read_file", description="读取文件")],
                )
            )
        ]
        return client, events

    client, events = asyncio.run(collect_events())

    assert client.completions.last_params["stream"] is True
    assert "tools" in client.completions.last_params
    assert [event.kind for event in events] == [
        "message_started",
        "tool_call_started",
        "tool_call_delta",
        "tool_call_delta",
        "tool_call_completed",
        "message_completed",
    ]
    assert [event.arguments_delta for event in events if event.kind == "tool_call_delta"] == [
        '{"path"',
        ': "README.md"}',
    ]
    completed = [event.tool_call for event in events if event.kind == "tool_call_completed"][0]
    assert completed is not None
    assert completed.id == "call_1"
    assert completed.name == "read_file"
    assert completed.arguments == {"path": "README.md"}
    assert events[-1].response is not None
    assert events[-1].response.tool_calls == [completed]
    assert events[-1].response.finish_reason == "tool_calls"


def test_openai_compatible_provider_does_not_complete_truncated_streaming_tool_call():
    async def collect_events():
        provider = OpenAICompatibleProvider(
            name="test-openai",
            model="test-model",
            api_key="test-key",
            client=_FakeOpenAITruncatedToolStreamClient(),
        )

        return [
            event
            async for event in provider.astream(ChatRequest(messages=[ChatMessage(role="user", content="读取 README")]))
        ]

    events = asyncio.run(collect_events())

    assert "tool_call_completed" not in [event.kind for event in events]
    assert events[-1].response is not None
    assert events[-1].response.finish_reason == "length"
    assert events[-1].response.tool_calls == []
    assert events[-1].response.diagnostics.warnings


def test_openai_compatible_provider_accumulates_multiple_streaming_tool_calls_by_index():
    async def collect_events():
        provider = OpenAICompatibleProvider(
            name="test-openai",
            model="test-model",
            api_key="test-key",
            client=_FakeOpenAIMultiToolStreamClient(),
        )

        return [
            event
            async for event in provider.astream(ChatRequest(messages=[ChatMessage(role="user", content="查找")]))
        ]

    events = asyncio.run(collect_events())

    completed = [event.tool_call for event in events if event.kind == "tool_call_completed"]
    assert len(completed) == 2
    assert completed[0] is not None
    assert completed[1] is not None
    assert completed[0].id == "call_a"
    assert completed[0].arguments == {"path": "README.md"}
    assert completed[1].id == "call_b"
    assert completed[1].arguments == {"pattern": "TODO"}


def test_openai_compatible_provider_emits_error_for_invalid_streaming_tool_arguments():
    async def collect_events():
        provider = OpenAICompatibleProvider(
            name="test-openai",
            model="test-model",
            api_key="test-key",
            client=_FakeOpenAIInvalidToolStreamClient(),
        )

        return [
            event
            async for event in provider.astream(ChatRequest(messages=[ChatMessage(role="user", content="读取 README")]))
        ]

    events = asyncio.run(collect_events())

    assert "error" in [event.kind for event in events]
    assert "tool_call_completed" not in [event.kind for event in events]
    assert events[-1].response is not None
    assert events[-1].response.tool_calls == []
    assert events[-1].response.diagnostics.warnings


def test_openai_compatible_provider_rejects_streaming_tool_call_without_arguments():
    async def collect_events():
        provider = OpenAICompatibleProvider(
            name="test-openai",
            model="test-model",
            api_key="test-key",
            client=_FakeOpenAIMissingArgumentsToolStreamClient(),
        )

        return [
            event
            async for event in provider.astream(ChatRequest(messages=[ChatMessage(role="user", content="读取 README")]))
        ]

    events = asyncio.run(collect_events())

    assert "error" in [event.kind for event in events]
    assert "tool_call_completed" not in [event.kind for event in events]
    assert events[-1].response is not None
    assert events[-1].response.tool_calls == []
    assert events[-1].response.diagnostics.warnings


def test_openai_compatible_provider_only_completes_streaming_tools_on_tool_calls_finish():
    async def collect_events():
        provider = OpenAICompatibleProvider(
            name="test-openai",
            model="test-model",
            api_key="test-key",
            client=_FakeOpenAIStopToolStreamClient(),
        )

        return [
            event
            async for event in provider.astream(ChatRequest(messages=[ChatMessage(role="user", content="读取 README")]))
        ]

    events = asyncio.run(collect_events())

    assert "error" in [event.kind for event in events]
    assert "tool_call_completed" not in [event.kind for event in events]
    assert events[-1].response is not None
    assert events[-1].response.finish_reason == "stop"
    assert events[-1].response.tool_calls == []


def test_openai_compatible_provider_raises_for_stream_error_chunk():
    async def collect_events():
        provider = OpenAICompatibleProvider(
            name="test-openai",
            model="test-model",
            api_key="test-key",
            client=_FakeOpenAIErrorChunkStreamClient(),
        )

        events = []
        with pytest.raises(ProviderError) as exc_info:
            async for event in provider.astream(ChatRequest(messages=[ChatMessage(role="user", content="hi")])):
                events.append(event)
        return events, exc_info.value

    events, error = asyncio.run(collect_events())

    assert error.kind == ProviderErrorKind.RATE_LIMIT
    assert [event.kind for event in events] == ["message_started", "error"]


def test_openai_compatible_provider_wraps_stream_iteration_error():
    async def collect_events():
        provider = OpenAICompatibleProvider(
            name="test-openai",
            model="test-model",
            api_key="test-key",
            client=_FakeOpenAIFailingStreamClient(),
        )

        events = []
        with pytest.raises(ProviderError) as exc_info:
            async for event in provider.astream(ChatRequest(messages=[ChatMessage(role="user", content="hi")])):
                events.append(event)
        return events, exc_info.value

    events, error = asyncio.run(collect_events())

    assert error.kind == ProviderErrorKind.SERVER_ERROR
    assert [event.kind for event in events] == ["message_started"]


def test_openai_compatible_provider_rejects_streaming_when_capability_disabled():
    async def collect_stream_error() -> None:
        provider = OpenAICompatibleProvider(
            name="test-openai",
            model="test-model",
            api_key="test-key",
            client=_FakeOpenAITextStreamClient(),
            capabilities=ProviderCapabilities(supports_streaming=False),
        )

        events = provider.astream(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
        with pytest.raises(ProviderError) as exc_info:
            async for _event in events:
                pass
        assert exc_info.value.kind == ProviderErrorKind.UNSUPPORTED

    asyncio.run(collect_stream_error())


def test_anthropic_provider_parses_text_and_tool_calls():
    client = _FakeAnthropicClient()
    provider = AnthropicProvider(model="claude-test", api_key="test-key", client=client)

    assert provider.name == "anthropic"

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
    assert response.usage is not None
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 8
    assert response.usage.total_tokens == 20


def test_anthropic_provider_serializes_forced_tool_choice():
    client = _FakeAnthropicClient()
    provider = AnthropicProvider(model="claude-test", api_key="test-key", client=client)

    provider.complete(
        ChatRequest(
            messages=[ChatMessage(role="user", content="读取 README")],
            tools=[ToolDefinition(name="read_file", description="读取文件")],
            tool_choice=ToolChoiceFunction(name="read_file"),
        )
    )

    assert client.messages.last_params["tool_choice"] == {"type": "tool", "name": "read_file"}


def test_anthropic_provider_maps_required_and_disables_parallel_when_unsupported():
    client = _FakeAnthropicClient()
    provider = AnthropicProvider(
        model="claude-test",
        api_key="test-key",
        client=client,
        capabilities=ProviderCapabilities(
            supports_streaming=True,
            supports_forced_tool_choice=True,
            supports_parallel_tool_calls=False,
        ),
    )

    provider.complete(
        ChatRequest(
            messages=[ChatMessage(role="user", content="读取 README")],
            tools=[ToolDefinition(name="read_file", description="读取文件")],
            tool_choice="required",
        )
    )

    assert client.messages.last_params["tool_choice"] == {
        "type": "any",
        "disable_parallel_tool_use": True,
    }


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


def test_anthropic_provider_merges_consecutive_tool_results():
    client = _FakeAnthropicClient()
    provider = AnthropicProvider(model="claude-test", api_key="test-key", client=client)

    provider.complete(
        ChatRequest(
            messages=[
                ChatMessage(role="tool", content="a", tool_call_id="toolu_1"),
                ChatMessage(role="tool", content="b", tool_call_id="toolu_2"),
            ]
        )
    )

    messages = client.messages.last_params["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert [block["tool_use_id"] for block in messages[0]["content"]] == ["toolu_1", "toolu_2"]


def test_anthropic_provider_parses_usage_and_discards_tools_on_length():
    provider = AnthropicProvider(model="claude-test", api_key="test-key", client=_FakeAnthropicLengthClient())

    response = provider.complete(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))

    assert response.finish_reason == "length"
    assert response.tool_calls == []
    assert response.usage is not None
    assert response.usage.input_tokens == 5
    assert response.usage.output_tokens == 9
    assert response.usage.total_tokens == 14
    assert any("length" in warning for warning in response.diagnostics.warnings)


def test_anthropic_provider_streams_text_deltas_and_final_response():
    async def collect_events():
        client = _FakeAnthropicTextStreamClient()
        provider = AnthropicProvider(model="claude-test", api_key="test-key", client=client)
        events = [
            event
            async for event in provider.astream(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
        ]
        return client, events

    client, events = asyncio.run(collect_events())

    assert client.messages.last_params["stream"] is True
    assert [event.kind for event in events] == [
        "message_started",
        "text_delta",
        "text_delta",
        "message_completed",
    ]
    assert [event.text for event in events if event.kind == "text_delta"] == ["你", "好"]
    assert events[-1].response is not None
    assert events[-1].response.content == "你好"
    assert events[-1].response.finish_reason == "stop"
    assert events[-1].response.usage is not None
    assert events[-1].response.usage.input_tokens == 3
    assert events[-1].response.usage.output_tokens == 2


def test_anthropic_provider_closes_stream_when_consumer_stops_early():
    async def consume_one_event():
        client = _ClosableAnthropicStreamClient()
        provider = AnthropicProvider(model="claude-test", api_key="test-key", client=client)
        events = provider.astream(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
        await anext(events)
        await anext(events)
        await events.aclose()
        return client.messages.stream

    stream = asyncio.run(consume_one_event())
    assert stream.close_count == 1


def test_anthropic_provider_streams_reasoning_and_tool_calls():
    async def collect_events():
        client = _FakeAnthropicClient()
        provider = AnthropicProvider(model="claude-test", api_key="test-key", client=client)
        events = [
            event
            async for event in provider.astream(
                ChatRequest(
                    messages=[ChatMessage(role="user", content="读取 README")],
                    tools=[ToolDefinition(name="read_file", description="读取文件")],
                )
            )
        ]
        return client, events

    client, events = asyncio.run(collect_events())

    assert client.messages.last_params["stream"] is True
    assert "tools" in client.messages.last_params
    kinds = [event.kind for event in events]
    assert kinds[0] == "message_started"
    assert "text_delta" in kinds
    assert "tool_call_started" in kinds
    assert "tool_call_delta" in kinds
    assert "tool_call_completed" in kinds
    assert kinds[-1] == "message_completed"
    completed = [event.tool_call for event in events if event.kind == "tool_call_completed"][0]
    assert completed is not None
    assert completed.id == "toolu_1"
    assert completed.arguments == {"path": "README.md"}
    assert events[-1].response is not None
    assert events[-1].response.finish_reason == "tool_calls"


def test_anthropic_provider_streams_thinking_deltas_into_diagnostics():
    async def collect_events():
        provider = AnthropicProvider(
            model="claude-test",
            api_key="test-key",
            client=_FakeAnthropicThinkingStreamClient(),
        )
        return [
            event
            async for event in provider.astream(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
        ]

    events = asyncio.run(collect_events())
    assert [event.kind for event in events] == [
        "message_started",
        "reasoning_delta",
        "reasoning_delta",
        "text_delta",
        "message_completed",
    ]
    assert events[-1].response is not None
    assert events[-1].response.diagnostics.reasoning == "先想一下"


def test_anthropic_provider_does_not_complete_truncated_streaming_tool_call():
    async def collect_events():
        provider = AnthropicProvider(
            model="claude-test",
            api_key="test-key",
            client=_FakeAnthropicTruncatedToolStreamClient(),
        )
        return [
            event
            async for event in provider.astream(ChatRequest(messages=[ChatMessage(role="user", content="读取 README")]))
        ]

    events = asyncio.run(collect_events())
    assert "tool_call_completed" not in [event.kind for event in events]
    assert events[-1].response is not None
    assert events[-1].response.tool_calls == []
    assert events[-1].response.finish_reason == "length"


def test_anthropic_provider_rejects_streaming_when_capability_disabled():
    provider = AnthropicProvider(
        model="claude-test",
        api_key="test-key",
        client=_FakeAnthropicClient(),
        capabilities=ProviderCapabilities(supports_streaming=False),
    )

    async def collect_stream_error():
        events = provider.astream(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
        with pytest.raises(ProviderError) as exc_info:
            await anext(events)
        return exc_info.value

    error = asyncio.run(collect_stream_error())
    assert error.kind == ProviderErrorKind.UNSUPPORTED
