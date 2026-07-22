from __future__ import annotations

from pathlib import Path

import pytest

from firstcoder.mcp.config_store import McpConfigStore, McpConfigStoreError


def test_add_local_preserves_existing_configuration_and_comments(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '# Keep this comment\ndefault_model = "fake/model"\n\n[providers.fake]\ntype = "openai-compatible"\n\n[models."fake/model"]\n',
        encoding="utf-8",
    )

    McpConfigStore(path).add_local("everything", ["npx", "-y", "server"], env={"TOKEN": "{env:TOKEN}"})

    content = path.read_text(encoding="utf-8")
    assert "# Keep this comment" in content
    assert 'default_model = "fake/model"' in content
    assert "[mcp.everything]" in content
    assert 'command = ["npx", "-y", "server"]' in content
    assert 'TOKEN = "{env:TOKEN}"' in content


def test_add_remote_replaces_same_named_server_and_lists_safe_summary(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    store = McpConfigStore(path)
    store.add_local("search", ["old-server"])

    store.add_remote(
        "search",
        "https://example.test/mcp",
        headers={"Authorization": "Bearer {env:SEARCH_TOKEN}"},
    )

    assert store.list_servers() == (
        {
            "name": "search",
            "type": "remote",
            "endpoint": "https://example.test/mcp",
            "enabled": True,
        },
    )
    assert "SEARCH_TOKEN" not in str(store.list_servers())
    assert "old-server" not in path.read_text(encoding="utf-8")


def test_add_remote_stores_bearer_token_environment_variable_without_token_value(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"

    McpConfigStore(path).add_remote(
        "github",
        "https://example.test/mcp",
        bearer_token_env_var="GITHUB_PAT_TOKEN",
    )

    content = path.read_text(encoding="utf-8")
    assert 'bearer_token_env_var = "GITHUB_PAT_TOKEN"' in content
    assert "GITHUB_PAT_TOKEN" not in str(McpConfigStore(path).list_servers())


def test_remove_returns_false_for_missing_server_and_true_after_removal(tmp_path: Path) -> None:
    store = McpConfigStore(tmp_path / "config.toml")
    store.add_local("echo", ["echo-server"])

    assert store.remove("missing") is False
    assert store.remove("echo") is True
    assert store.list_servers() == ()


@pytest.mark.parametrize("name", ["", "bad name", "bad.name"])
def test_store_rejects_unsafe_server_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(McpConfigStoreError, match="名称"):
        McpConfigStore(tmp_path / "config.toml").add_local(name, ["server"])


def test_store_rejects_empty_local_command_and_non_http_remote_url(tmp_path: Path) -> None:
    store = McpConfigStore(tmp_path / "config.toml")

    with pytest.raises(McpConfigStoreError, match="command"):
        store.add_local("local", [])
    with pytest.raises(McpConfigStoreError, match="HTTP"):
        store.add_remote("remote", "ftp://example.test/mcp")
