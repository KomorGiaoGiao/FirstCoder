"""内存版权限授权匹配。

第一版只做可测试的匹配逻辑。持久化 `.firstcoder/permissions.json` 会在后续阶段
接入同一组 `PermissionGrant` 类型。
"""

from __future__ import annotations

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
        if request.action == PermissionAction.EXECUTE_SHELL and _has_shell_control_operator(request.target):
            return False
        return _command_matches_prefix(request.target, grant.scope_value)
    if grant.scope_type == PermissionScopeType.HOST:
        return _host_from_target(request.target) == grant.scope_value.lower()
    if grant.scope_type == PermissionScopeType.ENV_KEY:
        return request.target.upper() == grant.scope_value.upper()
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
