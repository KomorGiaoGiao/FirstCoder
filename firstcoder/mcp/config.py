"""MCP TOML 配置提取与校验。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from firstcoder.mcp.models import McpConfigError, McpLocalServerConfig, McpRemoteServerConfig

if TYPE_CHECKING:
    from firstcoder.config.settings import AppConfig


_ENV_PLACEHOLDER = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_ALLOWED_TOOL_NAME = re.compile(r"[A-Za-z0-9_*-]+")
_ENVIRONMENT_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def load_mcp_configs(app_config: AppConfig) -> tuple[McpLocalServerConfig | McpRemoteServerConfig, ...]:
    """读取并校验合并后的 MCP 服务器期望配置。"""

    raw_servers = app_config.mcp_config()
    configs: list[McpLocalServerConfig | McpRemoteServerConfig] = []
    for name, raw_server in raw_servers.items():
        configs.append(_parse_server(name, raw_server))
    return tuple(configs)


def resolve_environment_placeholders(value: Any, env: Mapping[str, str]) -> Any:
    """递归替换配置中的环境变量占位符，缺失时不泄露任何变量值。"""

    if isinstance(value, str):
        return _ENV_PLACEHOLDER.sub(lambda match: _environment_value(match.group(1), env), value)
    if isinstance(value, list):
        return [resolve_environment_placeholders(item, env) for item in value]
    if isinstance(value, tuple):
        return tuple(resolve_environment_placeholders(item, env) for item in value)
    if isinstance(value, Mapping):
        return {key: resolve_environment_placeholders(item, env) for key, item in value.items()}
    return value


def _environment_value(name: str, env: Mapping[str, str]) -> str:
    value = env.get(name)
    if value is None or value == "":
        raise McpConfigError(f"缺少环境变量：{name}")
    return value


def _parse_server(name: object, raw_server: object) -> McpLocalServerConfig | McpRemoteServerConfig:
    if not isinstance(name, str) or not name:
        raise McpConfigError("MCP 服务器名称必须是非空字符串")
    if not isinstance(raw_server, Mapping):
        raise McpConfigError(f"MCP 服务器 {name} 必须是配置表")

    server_type = raw_server.get("type")
    if server_type not in {"local", "remote"}:
        raise McpConfigError(f"MCP 服务器 {name} 的 type 必须是 local 或 remote")
    enabled = _bool(raw_server.get("enabled", True), name, "enabled")
    timeout_ms = _timeout(raw_server.get("timeout_ms", 5000), name)
    allowed_tools = _allowed_tools(raw_server.get("allowed_tools"), name)

    if server_type == "local":
        _reject_unknown_fields(raw_server, name, {"type", "command", "env", "enabled", "timeout_ms", "allowed_tools"})
        _reject_field(raw_server, name, "url")
        _reject_field(raw_server, name, "headers")
        return McpLocalServerConfig(
            name=name,
            command=_command(raw_server.get("command"), name),
            env=_string_mapping(raw_server.get("env", {}), name, "env"),
            enabled=enabled,
            timeout_ms=timeout_ms,
            allowed_tools=allowed_tools,
        )

    _reject_unknown_fields(raw_server, name, {"type", "url", "headers", "bearer_token_env_var", "enabled", "timeout_ms", "allowed_tools"})
    _reject_field(raw_server, name, "command")
    _reject_field(raw_server, name, "env")
    return McpRemoteServerConfig(
        name=name,
        url=_url(raw_server.get("url"), name),
        headers=_string_mapping(raw_server.get("headers", {}), name, "headers"),
        bearer_token_env_var=_environment_variable_name(raw_server.get("bearer_token_env_var"), name, "bearer_token_env_var"),
        enabled=enabled,
        timeout_ms=timeout_ms,
        allowed_tools=allowed_tools,
    )


def _reject_field(server: Mapping[str, object], name: str, field: str) -> None:
    if field in server:
        raise McpConfigError(f"MCP 服务器 {name} 不能同时配置 {field}")


def _reject_unknown_fields(server: Mapping[str, object], name: str, allowed_fields: set[str]) -> None:
    unknown_fields = sorted(str(field) for field in server if field not in allowed_fields)
    if unknown_fields:
        raise McpConfigError(f"MCP 服务器 {name} 包含未知配置字段：{', '.join(unknown_fields)}")


def _command(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        raise McpConfigError(f"MCP 服务器 {name} 的 command 必须是非空字符串列表")
    return tuple(value)


def _url(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise McpConfigError(f"MCP 服务器 {name} 的 url 必须是 HTTP URL")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise McpConfigError(f"MCP 服务器 {name} 的 url 必须是 HTTP URL")
    return value


def _string_mapping(value: object, name: str, field: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        raise McpConfigError(f"MCP 服务器 {name} 的 {field} 必须是字符串映射")
    return dict(value)


def _environment_variable_name(value: object, name: str, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _ENVIRONMENT_VARIABLE_NAME.fullmatch(value):
        raise McpConfigError(f"MCP 服务器 {name} 的 {field} 必须是有效环境变量名")
    return value


def _bool(value: object, name: str, field: str) -> bool:
    if not isinstance(value, bool):
        raise McpConfigError(f"MCP 服务器 {name} 的 {field} 必须是布尔值")
    return value


def _timeout(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise McpConfigError(f"MCP 服务器 {name} 的 timeout_ms 必须是正整数")
    return value


def _allowed_tools(value: object, name: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) or not _ALLOWED_TOOL_NAME.fullmatch(item) for item in value):
        raise McpConfigError(f"MCP 服务器 {name} 的 allowed_tools 必须是有效工具名列表")
    return tuple(value)
