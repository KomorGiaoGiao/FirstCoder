"""配置加载和 provider factory 的基础测试。"""

from __future__ import annotations

import pytest

from firstcoder.config import AppConfig, load_config
from firstcoder.config.settings import default_global_config_path, render_default_config
from firstcoder.providers.factory import ProviderConfigError, create_provider_from_config
from firstcoder.providers.openai_compatible import OpenAICompatibleProvider
from firstcoder.providers.presets import PROVIDER_PRESETS


def test_load_config_defaults_to_openai(monkeypatch):
    monkeypatch.delenv("FIRSTCODER_PROVIDER", raising=False)

    config = load_config(env={})

    assert config.provider_name == "openai"


def test_load_config_reads_project_firstcoder_toml(tmp_path, monkeypatch):
    monkeypatch.delenv("FIRSTCODER_PROVIDER", raising=False)
    (tmp_path / "firstcoder.toml").write_text(
        "\n".join(
            [
                'model = "custom/custom-model"',
                "[provider]",
                'type = "openai-compatible"',
                'name = "custom"',
                'base_url = "https://example.com/v1"',
                'api_key_env = "CUSTOM_API_KEY"',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(project_root=tmp_path, env={"CUSTOM_API_KEY": "test-key"})

    assert config.provider_name == "openai-compatible"
    assert config.get_config_value("model") == "custom/custom-model"
    assert config.get_provider_value("name") == "custom"
    assert config.get_provider_value("base_url") == "https://example.com/v1"
    assert config.project_config_path == tmp_path / "firstcoder.toml"


def test_environment_provider_overrides_project_config(tmp_path, monkeypatch):
    monkeypatch.setenv("FIRSTCODER_PROVIDER", "deepseek")
    (tmp_path / "firstcoder.toml").write_text(
        "\n".join(["[provider]", 'type = "openai-compatible"']),
        encoding="utf-8",
    )

    config = load_config(project_root=tmp_path)

    assert config.provider_name == "deepseek"


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
    assert provider.base_url == "https://api.deepseek.com"
    assert provider.capabilities.supports_tools is True


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


def test_create_provider_from_config_supports_toml_openai_compatible():
    config = AppConfig(
        provider_name="openai-compatible",
        env={"YURENAPI_API_KEY": "test-key"},
        project_config={
            "model": "yurenapi/gpt-5.5",
            "provider": {
                "type": "openai-compatible",
                "name": "yurenapi",
                "base_url": "https://yurenapi.cn/v1",
                "api_key_env": "YURENAPI_API_KEY",
            },
        },
    )

    provider = create_provider_from_config(config)

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.name == "yurenapi"
    assert provider.model == "gpt-5.5"
    assert provider.base_url == "https://yurenapi.cn/v1"


def test_create_provider_from_config_project_overrides_global_model():
    config = AppConfig(
        provider_name="openai-compatible",
        env={"YURENAPI_API_KEY": "test-key"},
        global_config={
            "model": "yurenapi/global-model",
            "provider": {
                "type": "openai-compatible",
                "name": "yurenapi",
                "base_url": "https://global.example/v1",
                "api_key_env": "YURENAPI_API_KEY",
            },
        },
        project_config={
            "model": "yurenapi/project-model",
            "provider": {
                "base_url": "https://project.example/v1",
            },
        },
    )

    provider = create_provider_from_config(config)

    assert provider.model == "project-model"
    assert provider.base_url == "https://project.example/v1"


def test_render_default_config_uses_api_key_env_not_plain_secret():
    content = render_default_config()

    assert "api_key_env" in content
    assert "api_key =" not in content


def test_default_global_config_path_respects_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert default_global_config_path() == tmp_path / "firstcoder" / "config.toml"


def test_openai_compatible_presets_have_constructable_metadata():
    expected = {
        "openai",
        "deepseek",
        "qwen",
        "moonshot",
        "zhipu",
        "openrouter",
        "ollama",
    }

    for name in expected:
        preset = PROVIDER_PRESETS[name]
        assert preset.kind == "openai-compatible"
        assert preset.name == name
        assert preset.api_key_env
        assert preset.model_env
        assert preset.default_model
        assert preset.capabilities.supports_tools is True


def test_create_provider_from_config_passes_openrouter_headers():
    config = AppConfig(
        provider_name="openrouter",
        env={
            "OPENROUTER_API_KEY": "test-key",
        },
    )

    provider = create_provider_from_config(config)

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url == "https://openrouter.ai/api/v1"
    assert provider.extra_headers["X-Title"] == "FirstCoder"
