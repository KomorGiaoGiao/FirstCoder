"""权限系统基础类型。

这些类型描述程序侧安全边界，不进入模型可见 tool schema。模型可以请求动作，
但是否允许执行由这些结构和 `PermissionManager` 决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal


class PermissionAction(StrEnum):
    """工具想访问的能力类型。"""

    READ_PATH = "read_path"
    WRITE_PATH = "write_path"
    DELETE_PATH = "delete_path"
    EXECUTE_SHELL = "execute_shell"
    NETWORK_REQUEST = "network_request"
    GIT_OPERATION = "git_operation"
    READ_ENV = "read_env"


class PermissionMode(StrEnum):
    """用户当前希望默认策略采取的保守程度。"""

    CONSERVATIVE = "conservative"
    STANDARD = "standard"
    AGGRESSIVE = "aggressive"
    BYPASS = "bypass"


class PermissionDecisionKind(StrEnum):
    """一次权限预检的结论。"""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionPersistence(StrEnum):
    """决策的生效范围。"""

    ONCE = "once"
    SESSION = "session"
    ALWAYS = "always"


class PermissionScopeType(StrEnum):
    """`allow always` / `deny always` 可以匹配的授权范围。"""

    EXACT_PATH = "exact_path"
    PATH_TREE = "path_tree"
    COMMAND_PREFIX = "command_prefix"
    HOST = "host"
    ENV_KEY = "env_key"


class PermissionConfirmationChoice(StrEnum):
    """用户在权限确认 UI 中可以选择的动作。"""

    DENY = "deny"
    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS_SAME_SCOPE = "allow_always_same_scope"


@dataclass(slots=True)
class PermissionRequest:
    """描述工具准备执行的动作。"""

    id: str
    action: PermissionAction
    target: str
    reason: str = ""
    cwd: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PermissionGrant:
    """用户显式保存的长期授权或拒绝规则。"""

    id: str
    effect: Literal["allow", "deny"]
    action: PermissionAction
    scope_type: PermissionScopeType
    scope_value: str
    created_at: str
    reason: str = ""


@dataclass(slots=True)
class PermissionDecision:
    """权限预检结果。

    `ASK` 只表示需要暂停等待用户选择；工具执行层不能在 `ASK` 时继续执行。
    """

    kind: PermissionDecisionKind
    persistence: PermissionPersistence = PermissionPersistence.ONCE
    reason: str = ""
    grant: PermissionGrant | None = None
