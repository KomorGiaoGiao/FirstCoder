"""配置加载和 provider factory 的基础测试。"""

from __future__ import annotations

import pytest

from firstcoder.config import AppConfig, load_config
from firstcoder.providers.factory import ProviderConfigError, create_provider_from_config
from firstcoder.providers.openai_compatible import OpenAICompatibleProvider


def test_load_config_defaults_to_openai(monkeypatch):
    monkeypatch.delenv("FIRSTCODER_PROVIDER", raising=False)

    config = load_config()

    assert config.provider_name == "openai"


def test_load_config_argument_overrides_environment(monkeypatch):
    monkeypatch.setenv("FIRSTCODER_PROVIDER", "openai")

    config = load_config("deepseek")

    assert config.provider_name == "deepseek"


def test_create_provider_from_config_uses_preset_values():
    config = AppConfig(
        provider_name="deepseek",
        env={
            "DEEPSEEK_API_KEY": "test-key",
        },
    )

    provider = create_provider_from_config(config)

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.name == "deepseek"
    assert provider.model == "deepseek-chat"


def test_create_provider_from_config_reports_missing_api_key():
    config = AppConfig(provider_name="openai", env={})

    with pytest.raises(ProviderConfigError, match="OPENAI_API_KEY"):
        create_provider_from_config(config)


def test_create_provider_from_config_supports_custom_openai_compatible():
    config = AppConfig(
        provider_name="custom",
        env={
            "FIRSTCODER_API_KEY": "test-key",
            "FIRSTCODER_MODEL": "custom-model",
            "FIRSTCODER_BASE_URL": "https://example.com/v1",
            "FIRSTCODER_PROVIDER_NAME": "example",
        },
    )

    provider = create_provider_from_config(config)

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.name == "example"
    assert provider.model == "custom-model"
