"""权限统一决策入口。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from firstcoder.agent.user_input import UserInputOption, UserInputRequest
from firstcoder.permissions.grants import PermissionGrantStore
from firstcoder.permissions.policy import DefaultPermissionPolicy
from firstcoder.permissions.types import (
    PermissionAction,
    PermissionConfirmationChoice,
    PermissionDecision,
    PermissionDecisionKind,
    PermissionGrant,
    PermissionMode,
    PermissionPersistence,
    PermissionRequest,
    PermissionScopeType,
)


class PermissionManager:
    """组合长期授权和默认策略。

    后续阶段的用户确认、pending tool execution 和持久化都会接在这个入口后面；
    第一阶段先保证所有权限请求都能通过同一个纯函数式预检路径。
    """

    def __init__(
        self,
        *,
        policy: DefaultPermissionPolicy,
        grants: PermissionGrantStore | None = None,
        mode: PermissionMode = PermissionMode.STANDARD,
    ) -> None:
        self.policy = policy
        self.grants = grants or PermissionGrantStore()
        self.mode = mode

    def preflight(self, request: PermissionRequest) -> PermissionDecision:
        request = self.normalize_request(request)
        grant_decision = self.grants.matching_decision(request)
        if grant_decision is not None:
            return grant_decision
        return self.policy.decide(request, mode=self.mode)

    def build_confirmation(self, request: PermissionRequest) -> UserInputRequest:
        """把 `ASK` 权限请求转换成 UI 可展示的结构化用户输入请求。"""

        request = self.normalize_request(request)
        scope = default_scope_for_request(request, project_root=self.policy.project_root)
        question = _question_for_request(request)
        return UserInputRequest(
            id=request.id,
            kind="permission_confirmation",
            question=question,
            options=[
                UserInputOption(id=PermissionConfirmationChoice.DENY.value, label="Deny"),
                UserInputOption(id=PermissionConfirmationChoice.ALLOW_ONCE.value, label="Allow once"),
                *(
                    [
                        UserInputOption(
                            id=PermissionConfirmationChoice.ALLOW_ALWAYS_SAME_SCOPE.value,
                            label="Allow always",
                            description=f"{scope.scope_type.value}: {scope.scope_value}",
                        )
                    ]
                    if _allow_always_enabled(request)
                    else []
                ),
            ],
            payload={
                "request_type": "permission_confirmation",
                "permission_request_id": request.id,
                "action": request.action.value,
                "target": request.target,
                "reason": request.reason,
                "scope_type": scope.scope_type.value,
                "scope_value": scope.scope_value,
                "allow_always": _allow_always_enabled(request),
            },
        )

    def resolve_confirmation(self, request: PermissionRequest, choice: str) -> PermissionDecision:
        """解析用户选择，并在 allow always 时写入内存 grant。"""

        request = self.normalize_request(request)
        normalized = _normalize_choice(choice)
        if normalized == PermissionConfirmationChoice.DENY:
            return PermissionDecision(
                kind=PermissionDecisionKind.DENY,
                reason="用户拒绝了权限请求。",
            )
        if normalized == PermissionConfirmationChoice.ALLOW_ONCE:
            guard = self._confirmation_guard(request)
            if guard is not None:
                return guard
            return PermissionDecision(
                kind=PermissionDecisionKind.ALLOW,
                persistence=PermissionPersistence.ONCE,
                reason="用户允许本次执行。",
            )
        if normalized == PermissionConfirmationChoice.ALLOW_ALWAYS_SAME_SCOPE:
            if not _allow_always_enabled(request):
                return PermissionDecision(
                    kind=PermissionDecisionKind.DENY,
                    reason="该权限请求不支持长期授权。",
                )
            guard = self._confirmation_guard(request)
            if guard is not None:
                return guard
            scope = default_scope_for_request(request, project_root=self.policy.project_root)
            grant = PermissionGrant(
                id=f"grant_{request.id}",
                effect="allow",
                action=request.action,
                scope_type=scope.scope_type,
                scope_value=scope.scope_value,
                created_at=datetime.now(timezone.utc).isoformat(),
                reason="用户选择 allow always。",
            )
            self.grants.add(grant)
            return PermissionDecision(
                kind=PermissionDecisionKind.ALLOW,
                persistence=PermissionPersistence.ALWAYS,
                reason="用户允许同范围后续执行。",
                grant=grant,
            )
        return PermissionDecision(
            kind=PermissionDecisionKind.DENY,
            reason=f"未知权限选择：{choice}",
        )

    def _confirmation_guard(self, request: PermissionRequest) -> PermissionDecision | None:
        """确认只能解析当前仍需要询问的请求。

        这里重新运行一次预检，防止调用方误把硬拒绝请求送进确认流程后创建
        allow always grant。后续接 pending registry 后，还会再用 pending id 绑定。
        """

        grant_decision = self.grants.matching_decision(request)
        if grant_decision is not None:
            return grant_decision

        policy_decision = self.policy.decide(request, mode=self.mode)
        if policy_decision.kind != PermissionDecisionKind.ASK:
            return policy_decision
        return None

    def normalize_request(self, request: PermissionRequest) -> PermissionRequest:
        """让 manager 入口中的相对路径解析和默认策略保持同一基准。"""

        if request.cwd is not None:
            if request.cwd.is_absolute():
                return request
            return replace(request, cwd=(self.policy.project_root / request.cwd).resolve())
        if request.action not in {
            PermissionAction.READ_PATH,
            PermissionAction.WRITE_PATH,
            PermissionAction.DELETE_PATH,
        }:
            return request
        return replace(request, cwd=self.policy.project_root)


class _PermissionScope:
    def __init__(self, *, scope_type: PermissionScopeType, scope_value: str) -> None:
        self.scope_type = scope_type
        self.scope_value = scope_value


def default_scope_for_request(request: PermissionRequest, *, project_root: Path | None = None) -> _PermissionScope:
    """为 allow always 生成第一版保守 scope。"""

    if request.action == PermissionAction.READ_PATH:
        return _PermissionScope(
            scope_type=PermissionScopeType.EXACT_PATH,
            scope_value=_path_scope_value(request, project_root=project_root),
        )
    if request.action == PermissionAction.WRITE_PATH:
        return _PermissionScope(
            scope_type=PermissionScopeType.EXACT_PATH,
            scope_value=_path_scope_value(request, project_root=project_root),
        )
    if request.action == PermissionAction.DELETE_PATH:
        return _PermissionScope(
            scope_type=PermissionScopeType.EXACT_PATH,
            scope_value=_path_scope_value(request, project_root=project_root),
        )
    if request.action == PermissionAction.EXECUTE_SHELL:
        return _PermissionScope(
            scope_type=PermissionScopeType.COMMAND_PREFIX,
            scope_value=_shell_command_scope(request.target),
        )
    if request.action == PermissionAction.NETWORK_REQUEST:
        return _PermissionScope(scope_type=PermissionScopeType.HOST, scope_value=_host_scope_value(request.target))
    if request.action == PermissionAction.READ_ENV:
        return _PermissionScope(scope_type=PermissionScopeType.ENV_KEY, scope_value=request.target.upper())
    if request.action == PermissionAction.GIT_OPERATION:
        return _PermissionScope(scope_type=PermissionScopeType.COMMAND_PREFIX, scope_value=_git_command_scope(request.target))
    if request.action == PermissionAction.MCP_TOOL:
        return _PermissionScope(scope_type=PermissionScopeType.MCP_TOOL, scope_value=request.target)
    return _PermissionScope(scope_type=PermissionScopeType.COMMAND_PREFIX, scope_value=request.target.strip())


def _path_scope_value(request: PermissionRequest, *, project_root: Path | None) -> str:
    path = Path(request.target)
    if not path.is_absolute():
        path = (request.cwd or project_root or Path.cwd()) / path
    return str(path.resolve())


def _command_prefix(command: str) -> str:
    parts = command.strip().split()
    if not parts:
        return ""
    if len(parts) >= 2 and parts[0] == "git":
        return " ".join(parts[:2])
    return parts[0]


def _shell_command_scope(command: str) -> str:
    """第一版 shell allow-always 用完整命令作 prefix，避免放大到裸解释器。"""

    return command.strip()


def _git_command_scope(command: str) -> str:
    """git_operation 的 target 按 git 子命令建模，例如 `status --short` -> `status`。"""

    return _command_prefix(command)


def _host_scope_value(target: str) -> str:
    parsed = urlparse(target)
    host = parsed.hostname or target.split("/", 1)[0].split(":", 1)[0]
    return host.rstrip(".").lower()


def _question_for_request(request: PermissionRequest) -> str:
    reason = f"\n原因：{request.reason}" if request.reason else ""
    return f"允许执行权限操作 `{request.action.value}` 吗？\n目标：{request.target}{reason}"


def _allow_always_enabled(request: PermissionRequest) -> bool:
    return bool(request.metadata.get("allow_always", True))


def _normalize_choice(choice: str) -> PermissionConfirmationChoice | None:
    normalized = choice.strip().lower()
    numeric = {
        "1": PermissionConfirmationChoice.DENY,
        "2": PermissionConfirmationChoice.ALLOW_ONCE,
        "3": PermissionConfirmationChoice.ALLOW_ALWAYS_SAME_SCOPE,
    }
    if normalized in numeric:
        return numeric[normalized]
    for item in PermissionConfirmationChoice:
        if normalized == item.value:
            return item
    return None
