"""模型 provider 抽象和实现入口。"""

from firstcoder.providers.anthropic_provider import AnthropicProvider
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.factory import ProviderConfigError, create_provider, create_provider_from_config
from firstcoder.providers.openai_compatible import OpenAICompatibleProvider
from firstcoder.providers.tool_adapters import to_anthropic_tool, to_openai_tool
from firstcoder.providers.types import ChatMessage, ChatRequest, ChatResponse, ToolCall, ToolDefinition

__all__ = [
    "AnthropicProvider",
    "ChatMessage",
    "ChatProvider",
    "ChatRequest",
    "ChatResponse",
    "OpenAICompatibleProvider",
    "ProviderConfigError",
    "ToolCall",
    "ToolDefinition",
    "create_provider",
    "create_provider_from_config",
    "to_anthropic_tool",
    "to_openai_tool",
]
