"""provider 构造入口。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from firstcoder.config import AppConfig, load_config
from firstcoder.config.models import ModelProfile
from firstcoder.providers.anthropic_provider import AnthropicProvider
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.openai_compatible import OpenAICompatibleProvider
from firstcoder.providers.presets import PROVIDER_PRESETS
from firstcoder.providers.types import ProviderCapabilities


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
    base_url = config.get_provider_value("base_url", env=preset.base_url_env, provider_name=preset.name) if preset.base_url_env else config.get_provider_value("base_url", provider_name=preset.name)
    base_url = base_url or preset.default_base_url

    return _create_provider_instance(
        kind=preset.kind,
        name=preset.name,
        model=model,
        api_key=api_key,
        base_url=base_url,
        capabilities=preset.capabilities,
        extra_headers=preset.extra_headers,
        extra_body=preset.extra_body,
    )


def create_provider_for_model(config: AppConfig, profile: ModelProfile) -> ChatProvider:
    """根据完整的模型 Profile 创建 provider。

    Profile 中的模型 ID、provider ID、endpoint 和能力覆盖属于当前模型选择，
    不应改写旧的单 provider factory 行为。
    """

    provider_type = profile.provider.type
    if provider_type in {"openai-compatible", "custom"}:
        return _create_catalog_openai_compatible(config, profile)
    if provider_type in PROVIDER_PRESETS:
        return _create_catalog_preset(config, profile)
    raise ProviderConfigError(f"不支持的 provider 类型：{provider_type}")


def _catalog_api_key(config: AppConfig, profile: ModelProfile, fallback_env: str | None) -> tuple[str | None, str]:
    env_name = profile.provider.api_key_env or fallback_env or "FIRSTCODER_API_KEY"
    return config.get_env(env_name), env_name


def _catalog_capabilities(base: ProviderCapabilities, profile: ModelProfile) -> ProviderCapabilities:
    overrides = {}
    if profile.provider.parallel_tool_calls is not None:
        overrides["supports_parallel_tool_calls"] = profile.provider.parallel_tool_calls
    if profile.provider.streaming is not None:
        overrides["supports_streaming"] = profile.provider.streaming
    return replace(base, **overrides) if overrides else base


def _create_catalog_openai_compatible(config: AppConfig, profile: ModelProfile) -> ChatProvider:
    api_key, env_name = _catalog_api_key(config, profile, "FIRSTCODER_API_KEY")
    if not api_key:
        raise ProviderConfigError(f"缺少环境变量：{env_name}")
    capabilities = _catalog_capabilities(ProviderCapabilities(supports_streaming=True), profile)
    return OpenAICompatibleProvider(
        name=profile.provider.id,
        model=profile.model_id,
        api_key=api_key,
        base_url=profile.provider.base_url,
        capabilities=capabilities,
    )


def _create_catalog_preset(config: AppConfig, profile: ModelProfile) -> ChatProvider:
    preset = PROVIDER_PRESETS[profile.provider.type]
    api_key, env_name = _catalog_api_key(config, profile, preset.api_key_env)
    if not api_key and preset.name == "ollama":
        api_key = "ollama"
    if not api_key:
        raise ProviderConfigError(f"缺少环境变量：{env_name}")
    base_url = profile.provider.base_url
    if base_url is None and preset.base_url_env:
        base_url = config.get_env(preset.base_url_env)
    base_url = base_url or preset.default_base_url
    capabilities = _catalog_capabilities(preset.capabilities, profile)
    return _create_provider_instance(
        kind=preset.kind,
        name=profile.provider.id,
        model=profile.model_id,
        api_key=api_key,
        base_url=base_url,
        capabilities=capabilities,
        extra_headers=preset.extra_headers,
        extra_body=preset.extra_body,
    )


def _create_provider_instance(
    *,
    kind: str,
    name: str,
    model: str,
    api_key: str,
    base_url: str | None,
    capabilities: ProviderCapabilities,
    extra_headers: Mapping[str, str] | None = None,
    extra_body: Mapping[str, Any] | None = None,
) -> ChatProvider:
    provider_class = {
        "openai-compatible": OpenAICompatibleProvider,
        "anthropic": AnthropicProvider,
    }.get(kind)
    if provider_class is None:
        raise ProviderConfigError(f"provider 类型未实现：{kind}")
    return provider_class(
        name=name,
        model=model,
        api_key=api_key,
        base_url=base_url,
        capabilities=capabilities,
        extra_headers=dict(extra_headers or {}),
        extra_body=dict(extra_body or {}),
    )


def _create_custom_openai_compatible(config: AppConfig) -> ChatProvider:
    """从环境变量或显式 provider 配置创建 OpenAI-compatible provider。"""

    provider_display_name = (
        config.get_provider_value(
            "name",
            env="FIRSTCODER_PROVIDER_NAME",
            default=config.provider_name,
        )
        or config.provider_name
    )
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
        capabilities=_openai_compatible_capabilities(config, provider_name=provider_display_name),
    )


def _openai_compatible_capabilities(config: AppConfig, *, provider_name: str) -> ProviderCapabilities:
    supports_parallel_tool_calls = config.get_provider_bool(
        "parallel_tool_calls",
        env="FIRSTCODER_PARALLEL_TOOL_CALLS",
        default=False,
        provider_name=provider_name,
    )
    return ProviderCapabilities(
        supports_streaming=True,
        supports_parallel_tool_calls=bool(supports_parallel_tool_calls),
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
    configured = config.get_config_value("default_model")
    if configured:
        if "/" in configured:
            configured_provider, configured_model = configured.split("/", 1)
            if configured_provider == provider_name:
                return configured_model
        else:
            return configured
    return default
