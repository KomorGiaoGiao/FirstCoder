"""配置加载和 provider factory 的基础测试。"""

from __future__ import annotations

import pytest

from firstcoder.config import AppConfig, load_config
from firstcoder.config.models import ModelCatalogError
from firstcoder.config.settings import default_global_config_path, render_default_config
from firstcoder.providers.anthropic_provider import AnthropicProvider
from firstcoder.providers.factory import (
    ProviderConfigError,
    create_provider_for_model,
    create_provider_from_config,
)
from firstcoder.providers.openai_compatible import OpenAICompatibleProvider
from firstcoder.providers.presets import PROVIDER_PRESETS


def test_load_config_defaults_to_openai(tmp_path, monkeypatch):
    monkeypatch.delenv("FIRSTCODER_PROVIDER", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

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


def test_create_provider_from_config_reads_parallel_tool_calls_capability():
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
                "parallel_tool_calls": True,
            },
        },
    )

    provider = create_provider_from_config(config)

    assert provider.capabilities.supports_parallel_tool_calls is True


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
    assert "parallel_tool_calls = true" in content


def test_default_global_config_path_respects_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert default_global_config_path() == tmp_path / "firstcoder" / "config.toml"


def test_mcp_config_merges_servers_without_using_provider_accessors():
    config = AppConfig(
        provider_name="openai",
        env={},
        global_config={"mcp": {"global": {"type": "local", "command": ["global"]}}},
        project_config={"mcp": {"project": {"type": "remote", "url": "https://example.test/mcp"}}},
    )

    assert config.mcp_config() == {
        "global": {"type": "local", "command": ["global"]},
        "project": {"type": "remote", "url": "https://example.test/mcp"},
    }


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


def test_model_catalog_deep_merges_global_and_project_entries() -> None:
    config = AppConfig(
        provider_name="openai-compatible",
        env={},
        global_config={
            "providers": {
                "yuren": {
                    "type": "openai-compatible",
                    "base_url": "https://global.example/v1",
                    "api_key_env": "YUREN_API_KEY",
                }
            },
            "models": {
                "yuren/gpt-main": {
                    "label": "Global label",
                    "request": {
                        "temperature": 0.2,
                        "extra_body": {"reasoning_effort": "medium", "reasoning_summary": "auto"},
                    },
                },
                "yuren/gpt-cheap": {},
            },
        },
        project_config={
            "default_model": "yuren/gpt-main",
            "providers": {"yuren": {"base_url": "https://project.example/v1"}},
            "models": {
                "yuren/gpt-main": {
                    "label": "Project label",
                    "request": {"max_tokens": 8192, "extra_body": {"reasoning_effort": "high"}},
                }
            },
        },
    )

    catalog = config.model_catalog()

    assert catalog.default_ref == "yuren/gpt-main"
    assert [item.ref for item in catalog.list()] == ["yuren/gpt-cheap", "yuren/gpt-main"]
    main = catalog.require("yuren/gpt-main")
    assert main.label == "Project label"
    assert main.provider.base_url == "https://project.example/v1"
    assert main.request.temperature == 0.2
    assert main.request.max_tokens == 8192
    assert main.request.extra_body == {"reasoning_effort": "high", "reasoning_summary": "auto"}


def test_model_catalog_rejects_model_without_declared_provider() -> None:
    config = AppConfig(provider_name="openai-compatible", env={}, project_config={"models": {"missing/model": {}}})

    with pytest.raises(ModelCatalogError, match="missing/model.*missing"):
        config.model_catalog()


def test_model_catalog_adapts_legacy_single_provider_config() -> None:
    config = AppConfig(
        provider_name="openai-compatible",
        env={"YUREN_API_KEY": "test-key"},
        project_config={
            "model": "yurenapi/gpt-legacy",
            "provider": {
                "type": "openai-compatible",
                "name": "yurenapi",
                "base_url": "https://example.test/v1",
                "api_key_env": "YUREN_API_KEY",
            },
        },
    )

    profile = config.model_catalog().require("yurenapi/gpt-legacy")

    assert profile.provider.type == "openai-compatible"
    assert profile.provider.base_url == "https://example.test/v1"


def test_create_provider_for_model_uses_profile_provider_and_model_options() -> None:
    config = AppConfig(
        provider_name="openai-compatible",
        env={"YUREN_API_KEY": "test-key"},
        project_config={
            "providers": {
                "yuren": {
                    "type": "openai-compatible",
                    "base_url": "https://example.test/v1",
                    "api_key_env": "YUREN_API_KEY",
                    "parallel_tool_calls": True,
                    "streaming": False,
                }
            },
            "models": {"yuren/gpt-test": {}},
        },
    )

    provider = create_provider_for_model(config, config.model_catalog().require("yuren/gpt-test"))

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.name == "yuren"
    assert provider.model == "gpt-test"
    assert provider.base_url == "https://example.test/v1"
    assert provider.capabilities.supports_parallel_tool_calls is True
    assert provider.capabilities.supports_streaming is False


def test_create_provider_for_model_supports_anthropic_profile() -> None:
    config = AppConfig(
        provider_name="anthropic",
        env={"ANTHROPIC_API_KEY": "test-key"},
        project_config={
            "providers": {"claude": {"type": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"}},
            "models": {"claude/claude-test": {}},
        },
    )

    provider = create_provider_for_model(config, config.model_catalog().require("claude/claude-test"))

    assert isinstance(provider, AnthropicProvider)
    assert provider.name == "claude"
    assert provider.model == "claude-test"


def test_create_provider_for_model_reports_profile_api_key_env() -> None:
    config = AppConfig(
        provider_name="openai-compatible",
        env={},
        project_config={
            "providers": {
                "yuren": {
                    "type": "openai-compatible",
                    "api_key_env": "YUREN_API_KEY",
                }
            },
            "models": {"yuren/gpt-test": {}},
        },
    )

    with pytest.raises(ProviderConfigError, match="YUREN_API_KEY"):
        create_provider_for_model(config, config.model_catalog().require("yuren/gpt-test"))


def test_create_provider_for_model_supports_preset_and_profile_model() -> None:
    config = AppConfig(
        provider_name="openai",
        env={"OPENAI_API_KEY": "test-key"},
        project_config={
            "providers": {"openai": {"type": "openai"}},
            "models": {"openai/custom-gpt": {}},
        },
    )

    provider = create_provider_for_model(config, config.model_catalog().require("openai/custom-gpt"))

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.model == "custom-gpt"


def test_create_provider_for_model_reports_missing_preset_api_key() -> None:
    config = AppConfig(
        provider_name="openai",
        env={},
        project_config={
            "providers": {"openai": {"type": "openai"}},
            "models": {"openai/custom-gpt": {}},
        },
    )

    with pytest.raises(ProviderConfigError, match="OPENAI_API_KEY"):
        create_provider_for_model(config, config.model_catalog().require("openai/custom-gpt"))


def test_model_catalog_validates_request_options_and_reserved_extra_body() -> None:
    base = {"providers": {"p": {"type": "openai-compatible"}}, "models": {"p/m": {}}}
    config = AppConfig(provider_name="p", env={}, project_config={**base, "models": {"p/m": {"request": {"max_tokens": 0}}}})
    with pytest.raises(ModelCatalogError, match="max_tokens"):
        config.model_catalog()

    config = AppConfig(
        provider_name="p",
        env={},
        project_config={
            **base,
            "models": {"p/m": {"request": {"extra_body": {"messages": []}}}},
        },
    )
    with pytest.raises(ModelCatalogError, match="extra_body"):
        config.model_catalog()
