"""provider 构造入口。"""

from __future__ import annotations

from firstcoder.config import AppConfig, load_config
from firstcoder.providers.anthropic_provider import AnthropicProvider
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.openai_compatible import OpenAICompatibleProvider
from firstcoder.providers.presets import PROVIDER_PRESETS


class ProviderConfigError(ValueError):
    """provider 配置缺失或不合法时抛出的异常。"""


def create_provider(provider_name: str | None = None, *, project_root=None) -> ChatProvider:
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

    config = load_config(provider_name, project_root=project_root)
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

    api_key = _provider_api_key(config, preset.api_key_env, provider_name=preset.name)
    if not api_key and preset.name == "ollama":
        # OpenAI SDK 要求 api_key 字段存在；Ollama 本地接口通常不会真正校验这个值。
        api_key = "ollama"
    if not api_key:
        raise ProviderConfigError(f"缺少环境变量：{preset.api_key_env}")

    model = _provider_model(config, preset.model_env, default=preset.default_model, provider_name=preset.name)
    base_url = (
        config.get_provider_value("base_url", env=preset.base_url_env, provider_name=preset.name)
        if preset.base_url_env
        else config.get_provider_value("base_url", provider_name=preset.name)
    )
    base_url = base_url or preset.default_base_url

    if preset.kind == "openai-compatible":
        return OpenAICompatibleProvider(
            name=preset.name,
            model=model,
            api_key=api_key,
            base_url=base_url,
            capabilities=preset.capabilities,
            extra_headers=preset.extra_headers,
            extra_body=preset.extra_body,
        )

    if preset.kind == "anthropic":
        return AnthropicProvider(model=model, api_key=api_key)

    raise ProviderConfigError(f"provider 类型未实现：{preset.kind}")


def _create_custom_openai_compatible(config: AppConfig) -> ChatProvider:
    """创建 OpenAI-compatible provider。

    兼容旧的 FIRSTCODER_* 环境变量，同时支持配置文件：

    model = "yurenapi/gpt-5.5"
    [provider]
    type = "openai-compatible"
    name = "yurenapi"
    base_url = "https://example.com/v1"
    api_key_env = "YURENAPI_API_KEY"
    """

    provider_display_name = config.get_provider_value(
        "name",
        env="FIRSTCODER_PROVIDER_NAME",
        default="openai-compatible",
    ) or "openai-compatible"
    api_key = _provider_api_key(config, "FIRSTCODER_API_KEY", provider_name=provider_display_name)
    if not api_key:
        configured_key_env = config.get_provider_value("api_key_env", provider_name=provider_display_name)
        missing = configured_key_env or "FIRSTCODER_API_KEY"
        raise ProviderConfigError(f"缺少环境变量：{missing}")

    model = _provider_model(config, "FIRSTCODER_MODEL", provider_name=provider_display_name)
    if not model:
        raise ProviderConfigError("缺少模型配置：FIRSTCODER_MODEL 或 config model")

    return OpenAICompatibleProvider(
        name=provider_display_name,
        model=model,
        api_key=api_key,
        base_url=config.get_provider_value("base_url", env="FIRSTCODER_BASE_URL", provider_name=provider_display_name),
    )


def _provider_api_key(config: AppConfig, fallback_env: str, *, provider_name: str) -> str | None:
    key = config.get_env(fallback_env)
    if key:
        return key
    configured_env = config.get_provider_value("api_key_env", provider_name=provider_name)
    if configured_env:
        return config.get_env(configured_env)
    return config.get_provider_value("api_key", provider_name=provider_name)


def _provider_model(
    config: AppConfig,
    fallback_env: str,
    *,
    provider_name: str,
    default: str | None = None,
) -> str | None:
    env_model = config.get_env(fallback_env)
    if env_model:
        return env_model
    configured = config.get_config_value("model")
    if configured:
        if "/" in configured:
            configured_provider, configured_model = configured.split("/", 1)
            if configured_provider == provider_name:
                return configured_model
        else:
            return configured
    return default
