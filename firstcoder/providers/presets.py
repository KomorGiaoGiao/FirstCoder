"""常见模型厂商的 provider 预设。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from firstcoder.providers.types import ProviderCapabilities

OPENAI_COMPATIBLE_CAPABILITIES = ProviderCapabilities(supports_streaming=True)


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    """从环境变量构造 provider 时使用的静态配置。"""

    name: str
    kind: str
    api_key_env: str
    model_env: str
    default_model: str
    base_url_env: str | None = None
    default_base_url: str | None = None
    capabilities: ProviderCapabilities = OPENAI_COMPATIBLE_CAPABILITIES
    extra_headers: dict[str, str] | None = None
    extra_body: dict[str, Any] | None = None


# 这里优先覆盖对 coding agent 学习项目最常见的几类接入方式。
# 大量厂商提供 OpenAI-compatible API，因此可以共用一个实现。
PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "openai": ProviderPreset(
        name="openai",
        kind="openai-compatible",
        api_key_env="OPENAI_API_KEY",
        model_env="OPENAI_MODEL",
        default_model="gpt-4.1-mini",
    ),
    "deepseek": ProviderPreset(
        name="deepseek",
        kind="openai-compatible",
        api_key_env="DEEPSEEK_API_KEY",
        model_env="DEEPSEEK_MODEL",
        default_model="deepseek-chat",
        base_url_env="DEEPSEEK_BASE_URL",
        default_base_url="https://api.deepseek.com",
    ),
    "qwen": ProviderPreset(
        name="qwen",
        kind="openai-compatible",
        api_key_env="DASHSCOPE_API_KEY",
        model_env="QWEN_MODEL",
        default_model="qwen-plus",
        base_url_env="DASHSCOPE_BASE_URL",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    "moonshot": ProviderPreset(
        name="moonshot",
        kind="openai-compatible",
        api_key_env="MOONSHOT_API_KEY",
        model_env="MOONSHOT_MODEL",
        default_model="moonshot-v1-8k",
        base_url_env="MOONSHOT_BASE_URL",
        default_base_url="https://api.moonshot.cn/v1",
    ),
    "zhipu": ProviderPreset(
        name="zhipu",
        kind="openai-compatible",
        api_key_env="ZHIPUAI_API_KEY",
        model_env="ZHIPU_MODEL",
        default_model="glm-4-flash",
        base_url_env="ZHIPU_BASE_URL",
        default_base_url="https://open.bigmodel.cn/api/paas/v4",
    ),
    "openrouter": ProviderPreset(
        name="openrouter",
        kind="openai-compatible",
        api_key_env="OPENROUTER_API_KEY",
        model_env="OPENROUTER_MODEL",
        default_model="openai/gpt-4.1-mini",
        base_url_env="OPENROUTER_BASE_URL",
        default_base_url="https://openrouter.ai/api/v1",
        extra_headers={
            "HTTP-Referer": "https://github.com/Komor-Code/FirstCoder",
            "X-Title": "FirstCoder",
        },
    ),
    "ollama": ProviderPreset(
        name="ollama",
        kind="openai-compatible",
        api_key_env="OLLAMA_API_KEY",
        model_env="OLLAMA_MODEL",
        default_model="qwen2.5-coder:7b",
        base_url_env="OLLAMA_BASE_URL",
        default_base_url="http://localhost:11434/v1",
    ),
    "anthropic": ProviderPreset(
        name="anthropic",
        kind="anthropic",
        api_key_env="ANTHROPIC_API_KEY",
        model_env="ANTHROPIC_MODEL",
        default_model="claude-sonnet-4-5",
        base_url_env="ANTHROPIC_BASE_URL",
        # 与 OpenAI-compatible 主线对齐：streaming + tools + forced tool_choice。
        capabilities=ProviderCapabilities(
            supports_streaming=True,
            supports_forced_tool_choice=True,
            supports_parallel_tool_calls=True,
        ),
    ),
}
