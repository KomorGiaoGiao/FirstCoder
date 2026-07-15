"""MCP 配置解析测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from firstcoder.config import AppConfig
from firstcoder.mcp.config import load_mcp_configs, resolve_environment_placeholders
from firstcoder.mcp.models import McpConfigError, McpLocalServerConfig, McpRemoteServerConfig


def test_load_mcp_configs_parses_local_server_with_defaults():
    config = AppConfig(
        provider_name="openai",
        env={},
        project_config={
            "mcp": {"lark": {"type": "local", "command": ["lark-mcp", "serve"]}}
        },
    )

    servers = load_mcp_configs(config)

    assert servers == (
        McpLocalServerConfig(
            name="lark",
            command=("lark-mcp", "serve"),
            env={},
            allowed_tools=None,
        ),
    )
    assert servers[0].enabled is True
    assert servers[0].timeout_ms == 5000
    with pytest.raises(FrozenInstanceError):
        servers[0].enabled = False


def test_load_mcp_configs_parses_remote_server():
    config = AppConfig(
        provider_name="openai",
        env={},
        project_config={
            "mcp": {
                "github": {
                    "type": "remote",
                    "url": "https://example.test/mcp",
                    "headers": {"Accept": "application/json"},
                    "enabled": False,
                    "timeout_ms": 8000,
                    "allowed_tools": ["issues_*", "pull_request_read"],
                }
            }
        },
    )

    servers = load_mcp_configs(config)

    assert servers == (
        McpRemoteServerConfig(
            name="github",
            url="https://example.test/mcp",
            headers={"Accept": "application/json"},
            enabled=False,
            timeout_ms=8000,
            allowed_tools=("issues_*", "pull_request_read"),
        ),
    )


@pytest.mark.parametrize(
    "server",
    [
        {"type": "local"},
        {"type": "remote"},
        {"type": "local", "command": ["server"], "url": "https://example.test/mcp"},
        {"type": "remote", "url": "https://example.test/mcp", "command": ["server"]},
    ],
)
def test_load_mcp_configs_rejects_missing_or_mixed_transport_fields(server):
    config = AppConfig(provider_name="openai", env={}, project_config={"mcp": {"bad": server}})

    with pytest.raises(McpConfigError):
        load_mcp_configs(config)


def test_project_server_completely_overrides_same_named_global_server():
    config = AppConfig(
        provider_name="openai",
        env={},
        global_config={
            "mcp": {
                "github": {
                    "type": "remote",
                    "url": "https://global.example/mcp",
                    "headers": {"X-Global": "yes"},
                },
                "global-only": {"type": "local", "command": ["global"]},
            }
        },
        project_config={"mcp": {"github": {"type": "local", "command": ["project"]}}},
    )

    assert config.mcp_config() == {
        "github": {"type": "local", "command": ["project"]},
        "global-only": {"type": "local", "command": ["global"]},
    }
    assert load_mcp_configs(config)[0] == McpLocalServerConfig(
        name="github", command=("project",), env={}, allowed_tools=None
    )


def test_resolve_environment_placeholders_reports_only_missing_variable_name():
    with pytest.raises(McpConfigError) as error:
        resolve_environment_placeholders(
            {"Authorization": "Bearer {env:GITHUB_TOKEN}"},
            {"OTHER_SECRET": "do-not-leak"},
        )

    assert str(error.value) == "缺少环境变量：GITHUB_TOKEN"
    assert "do-not-leak" not in str(error.value)


def test_resolve_environment_placeholders_recurses_without_mutating_input():
    value = {"headers": {"Authorization": "Bearer {env:TOKEN}"}, "args": ["{env:HOST}"]}

    resolved = resolve_environment_placeholders(value, {"TOKEN": "secret", "HOST": "example.test"})

    assert resolved == {
        "headers": {"Authorization": "Bearer secret"},
        "args": ["example.test"],
    }
    assert value["headers"]["Authorization"] == "Bearer {env:TOKEN}"


@pytest.mark.parametrize("allowed_tools", ["calendar_*", [""], ["valid", 1], ["bad tool"]])
def test_load_mcp_configs_validates_allowed_tools(allowed_tools):
    config = AppConfig(
        provider_name="openai",
        env={},
        project_config={
            "mcp": {
                "lark": {
                    "type": "local",
                    "command": ["lark-mcp"],
                    "allowed_tools": allowed_tools,
                }
            }
        },
    )

    with pytest.raises(McpConfigError, match="allowed_tools"):
        load_mcp_configs(config)
