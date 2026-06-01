"""provider 构造入口。"""

from __future__ import annotations

from firstcoder.config import AppConfig, load_config
from firstcoder.providers.anthropic_provider import AnthropicProvider
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.openai_compatible import OpenAICompatibleProvider
from firstcoder.providers.presets import PROVIDER_PRESETS


class ProviderConfigError(ValueError):
    """provider 配置缺失或不合法时抛出的异常。"""


def create_provider(provider_name: str | None = None) -> ChatProvider:
    """根据应用配置创建 provider。

    优先级：
    1. 函数参数 `provider_name`
    2. 环境变量 `FIRSTCODER_PROVIDER`
    3. 默认 `openai`

    自定义 OpenAI-compatible 接口可以使用：
    - `FIRSTCODER_PROVIDER=openai-compatible`
    - `FIRSTCODER_API_KEY`
    - `FIRSTCODER_BASE_URL`
    - `FIRSTCODER_MODEL`
    """

    config = load_config(provider_name)
    return create_provider_from_config(config)


def create_provider_from_config(config: AppConfig) -> ChatProvider:
    """根据已经加载好的应用配置创建 provider。

    这一层只关心 provider 相关规则：选择哪个 provider、读取该 provider 需要的
    API key / model / base_url，并实例化对应的具体 provider。
    """

    selected = config.provider_name
    if selected in {"openai-compatible", "custom"}:
        return _create_custom_openai_compatible(config)

    preset = PROVIDER_PRESETS.get(selected)
    if preset is None:
        supported = ", ".join(sorted([*PROVIDER_PRESETS.keys(), "openai-compatible", "custom"]))
        raise ProviderConfigError(f"不支持的 provider：{selected}。当前支持：{supported}")

    api_key = config.get_env(preset.api_key_env)
    if not api_key and preset.name == "ollama":
        # OpenAI SDK 要求 api_key 字段存在；Ollama 本地接口通常不会真正校验这个值。
        api_key = "ollama"
    if not api_key:
        raise ProviderConfigError(f"缺少环境变量：{preset.api_key_env}")

    model = config.get_env(preset.model_env) or preset.default_model
    base_url = config.get_env(preset.base_url_env) if preset.base_url_env else None
    base_url = base_url or preset.default_base_url

    if preset.kind == "openai-compatible":
        return OpenAICompatibleProvider(
            name=preset.name,
            model=model,
            api_key=api_key,
            base_url=base_url,
        )

    if preset.kind == "anthropic":
        return AnthropicProvider(model=model, api_key=api_key)

    raise ProviderConfigError(f"provider 类型未实现：{preset.kind}")


def _create_custom_openai_compatible(config: AppConfig) -> ChatProvider:
    """创建完全由 FIRSTCODER_* 环境变量配置的 OpenAI-compatible provider。"""

    api_key = config.get_env("FIRSTCODER_API_KEY")
    if not api_key:
        raise ProviderConfigError("缺少环境变量：FIRSTCODER_API_KEY")

    model = config.get_env("FIRSTCODER_MODEL")
    if not model:
        raise ProviderConfigError("缺少环境变量：FIRSTCODER_MODEL")

    return OpenAICompatibleProvider(
        name=config.get_env("FIRSTCODER_PROVIDER_NAME", "openai-compatible") or "openai-compatible",
        model=model,
        api_key=api_key,
        base_url=config.get_env("FIRSTCODER_BASE_URL"),
    )
