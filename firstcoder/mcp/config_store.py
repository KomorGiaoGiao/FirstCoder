"""MCP 配置文件的保真读写服务。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

import tomlkit
from tomlkit import TOMLDocument

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_ENVIRONMENT_VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class McpConfigStoreError(ValueError):
    """表示无法安全写入 MCP 配置。"""


class McpConfigStore:
    """只管理 TOML 的 ``[mcp.<name>]`` 表，并保留其他配置内容。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def add_local(
        self,
        name: str,
        command: list[str],
        *,
        env: Mapping[str, str] | None = None,
        enabled: bool = True,
        timeout_ms: int = 5_000,
    ) -> None:
        """新增或完整替换一个 local stdio MCP server。"""

        self._validate_name(name)
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise McpConfigStoreError("local MCP 的 command 必须是非空字符串列表")
        table = tomlkit.table()
        table.add("type", "local")
        table.add("command", command)
        table.add("enabled", enabled)
        table.add("timeout_ms", timeout_ms)
        if env:
            table.add("env", dict(env))
        self._replace_server(name, table)

    def add_remote(
        self,
        name: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        bearer_token_env_var: str | None = None,
        enabled: bool = True,
        timeout_ms: int = 5_000,
    ) -> None:
        """新增或完整替换一个 remote Streamable HTTP MCP server。"""

        self._validate_name(name)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise McpConfigStoreError("remote MCP 的 url 必须是 HTTP URL")
        if bearer_token_env_var is not None and not _ENVIRONMENT_VARIABLE_NAME.fullmatch(bearer_token_env_var):
            raise McpConfigStoreError("bearer_token_env_var 必须是有效环境变量名")
        table = tomlkit.table()
        table.add("type", "remote")
        table.add("url", url)
        table.add("enabled", enabled)
        table.add("timeout_ms", timeout_ms)
        if headers:
            table.add("headers", dict(headers))
        if bearer_token_env_var:
            table.add("bearer_token_env_var", bearer_token_env_var)
        self._replace_server(name, table)

    def remove(self, name: str) -> bool:
        """删除一个 server；不存在时返回 ``False``。"""

        self._validate_name(name)
        document = self._read_document()
        mcp = document.get("mcp")
        if not isinstance(mcp, dict) or name not in mcp:
            return False
        del mcp[name]
        self._write_document(document)
        return True

    def list_servers(self) -> tuple[dict[str, object], ...]:
        """返回可安全打印的配置摘要，永不返回 headers 或 env。"""

        document = self._read_document()
        mcp = document.get("mcp")
        if not isinstance(mcp, dict):
            return ()
        summary: list[dict[str, object]] = []
        for name, config in mcp.items():
            if not isinstance(name, str) or not isinstance(config, dict):
                continue
            server_type = config.get("type")
            if server_type not in {"local", "remote"}:
                continue
            endpoint = " ".join(str(item) for item in config.get("command", [])) if server_type == "local" else config.get("url", "")
            summary.append(
                {
                    "name": name,
                    "type": str(server_type),
                    "endpoint": str(endpoint),
                    "enabled": bool(config.get("enabled", True)),
                }
            )
        return tuple(sorted(summary, key=lambda item: str(item["name"])))

    def _replace_server(self, name: str, server: object) -> None:
        document = self._read_document()
        mcp = document.get("mcp")
        if mcp is None:
            mcp = tomlkit.table()
            document.add("mcp", mcp)
        if not isinstance(mcp, dict):
            raise McpConfigStoreError("[mcp] 配置必须是表")
        mcp[name] = server
        self._write_document(document)

    def _read_document(self) -> TOMLDocument:
        if not self.path.exists():
            return tomlkit.document()
        try:
            return tomlkit.parse(self.path.read_text(encoding="utf-8"))
        except Exception as error:  # noqa: BLE001
            raise McpConfigStoreError("无法解析 MCP 配置文件") from error

    def _write_document(self, document: TOMLDocument) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(tomlkit.dumps(document), encoding="utf-8")
        temporary.replace(self.path)

    @staticmethod
    def _validate_name(name: str) -> None:
        if not _SAFE_NAME.fullmatch(name):
            raise McpConfigStoreError("MCP server 名称只能包含字母、数字、_ 或 -")
