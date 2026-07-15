"""内存版权限授权匹配。

第一版只做可测试的匹配逻辑。持久化 `.firstcoder/permissions.json` 会在后续阶段
接入同一组 `PermissionGrant` 类型。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from firstcoder.permissions.types import (
    PermissionAction,
    PermissionDecision,
    PermissionDecisionKind,
    PermissionGrant,
    PermissionPersistence,
    PermissionRequest,
    PermissionScopeType,
)

_SHELL_CONTROL_PATTERN = re.compile(r"(&&|\|\||\$\(|[;&|<>`\r\n])")


class PermissionGrantStore:
    """保存并匹配长期授权。

    deny grant 永远优先于 allow grant，避免后续新增 allow 规则意外放开更小范围的
    明确拒绝。
    """

    def __init__(self, grants: list[PermissionGrant] | None = None) -> None:
        self._grants = list(grants or [])

    def add(self, grant: PermissionGrant) -> None:
        self._grants.append(grant)

    def list(self) -> list[PermissionGrant]:
        return list(self._grants)

    def matching_decision(self, request: PermissionRequest) -> PermissionDecision | None:
        matches = [grant for grant in self._grants if _grant_matches(grant, request)]
        if not matches:
            return None

        deny = next((grant for grant in matches if grant.effect == "deny"), None)
        if deny is not None:
            return PermissionDecision(
                kind=PermissionDecisionKind.DENY,
                persistence=PermissionPersistence.ALWAYS,
                reason=deny.reason or "命中长期拒绝授权。",
                grant=deny,
            )

        allow = next((grant for grant in matches if grant.effect == "allow"), None)
        if allow is None:
            return None
        return PermissionDecision(
            kind=PermissionDecisionKind.ALLOW,
            persistence=PermissionPersistence.ALWAYS,
            reason=allow.reason or "命中长期允许授权。",
            grant=allow,
        )


class FilePermissionGrantStore(PermissionGrantStore):
    """把 allow-always grant 持久化到项目数据目录。

    第一版使用一个小 JSON 文件，保持容易阅读和手工复盘。后续如果迁移到 SQLite，
    `PermissionGrantStore` 这层匹配接口可以继续保持不变。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        super().__init__(_load_grants(self.path))

    def add(self, grant: PermissionGrant) -> None:
        super().add(grant)
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "grants": [_grant_to_dict(grant) for grant in self.list()]}
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.path)


def _grant_matches(grant: PermissionGrant, request: PermissionRequest) -> bool:
    if grant.action != request.action:
        return False

    if grant.scope_type == PermissionScopeType.EXACT_PATH:
        return _canonical_path(grant.scope_value, cwd=request.cwd) == _canonical_path(request.target, cwd=request.cwd)
    if grant.scope_type == PermissionScopeType.PATH_TREE:
        root = Path(_canonical_path(grant.scope_value, cwd=request.cwd))
        target = Path(_canonical_path(request.target, cwd=request.cwd))
        return target == root or root in target.parents
    if grant.scope_type == PermissionScopeType.COMMAND_PREFIX:
        if request.action in {PermissionAction.EXECUTE_SHELL, PermissionAction.GIT_OPERATION}:
            if _has_shell_control_operator(request.target):
                return False
        if request.action == PermissionAction.EXECUTE_SHELL and _has_shell_control_operator(request.target):
            return False
        return _command_matches_prefix(request.target, grant.scope_value)
    if grant.scope_type == PermissionScopeType.HOST:
        return _host_from_target(request.target) == grant.scope_value.lower()
    if grant.scope_type == PermissionScopeType.ENV_KEY:
        return request.target.upper() == grant.scope_value.upper()
    if grant.scope_type == PermissionScopeType.MCP_TOOL:
        return request.action == PermissionAction.MCP_TOOL and request.target == grant.scope_value
    return False


def _canonical_path(value: str, *, cwd: Path | None) -> str:
    path = Path(value)
    if not path.is_absolute() and cwd is not None:
        path = cwd / path
    return os.path.normcase(str(path.resolve()))


def _command_matches_prefix(command: str, prefix: str) -> bool:
    command = command.strip()
    prefix = prefix.strip()
    if not prefix:
        return False
    return command == prefix or command.startswith(prefix + " ")


def _has_shell_control_operator(command: str) -> bool:
    return bool(_SHELL_CONTROL_PATTERN.search(command))


def _host_from_target(target: str) -> str:
    parsed = urlparse(target)
    if parsed.hostname:
        return parsed.hostname.lower()
    return target.split("/", 1)[0].split(":", 1)[0].lower()


def _load_grants(path: Path) -> list[PermissionGrant]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw_grants = data.get("grants") if isinstance(data, dict) else data
    if not isinstance(raw_grants, list):
        return []
    grants: list[PermissionGrant] = []
    for item in raw_grants:
        if isinstance(item, dict):
            try:
                grants.append(_grant_from_dict(item))
            except (KeyError, TypeError, ValueError):
                continue
    return grants


def _grant_to_dict(grant: PermissionGrant) -> dict[str, str]:
    return {
        "id": grant.id,
        "effect": grant.effect,
        "action": grant.action.value,
        "scope_type": grant.scope_type.value,
        "scope_value": grant.scope_value,
        "created_at": grant.created_at,
        "reason": grant.reason,
    }


def _grant_from_dict(data: dict[str, object]) -> PermissionGrant:
    return PermissionGrant(
        id=str(data["id"]),
        effect=str(data["effect"]),  # type: ignore[arg-type]
        action=PermissionAction(str(data["action"])),
        scope_type=PermissionScopeType(str(data["scope_type"])),
        scope_value=str(data["scope_value"]),
        created_at=str(data["created_at"]),
        reason=str(data.get("reason") or ""),
    )
