"""provider 层的基础行为测试。"""

from __future__ import annotations

from firstcoder.providers.anthropic_provider import AnthropicProvider
from firstcoder.providers.openai_compatible import OpenAICompatibleProvider
from firstcoder.providers.types import ChatMessage, ChatRequest, ToolCall, ToolDefinition


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
