"""FirstCoder 默认权限策略。

默认策略只处理“没有命中显式 grant”的情况。它提供安全底线：激进模式可以减少
普通项目内写入确认，但不能绕过敏感环境变量、项目根外删除、敏感文件覆盖等硬边界。
"""

from __future__ import annotations

import re
from pathlib import Path

from firstcoder.permissions.types import (
    PermissionAction,
    PermissionDecision,
    PermissionDecisionKind,
    PermissionMode,
    PermissionRequest,
)

_SENSITIVE_ENV_KEYWORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE")
_SENSITIVE_FILENAMES = {".env"}
_SENSITIVE_SUFFIXES = {".pem", ".key"}
_READONLY_GIT_COMMANDS = {"status", "diff", "log"}
_AGGRESSIVE_ALLOWED_COMMANDS = (
    "pytest",
    "python -m pytest",
    "python3 -m pytest",
    "ruff",
    "mypy",
    "git status",
    "git diff",
    "git log",
    "git apply",
    "npm test",
    "pnpm test",
    "yarn test",
    "go test",
    "cargo test",
    "make test",
)
_AGGRESSIVE_ALLOWED_EXECUTABLES = ("python", "python3", "sqlite3")
_DANGEROUS_SHELL_PREFIXES = (
    "rm",
    "sudo",
    "curl",
    "wget",
    "chmod",
    "chown",
    "python -m pip",
    "python3 -m pip",
    "pip",
    "pip3",
)
_SHELL_CONTROL_PATTERN = re.compile(r"(&&|\|\||\$\(|[;&|<>`\r\n])")


class DefaultPermissionPolicy:
    """基于项目根目录的默认权限策略。"""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()

    def decide(self, request: PermissionRequest, *, mode: PermissionMode) -> PermissionDecision:
        if mode == PermissionMode.BYPASS:
            return self._allow("bypass 模式允许权限请求。")
        if request.action in {
            PermissionAction.READ_PATH,
            PermissionAction.WRITE_PATH,
            PermissionAction.DELETE_PATH,
        }:
            return self._decide_path(request, mode=mode)
        if request.action == PermissionAction.READ_ENV:
            return self._decide_env(request)
        if request.action == PermissionAction.GIT_OPERATION:
            return self._decide_git(request)
        if request.action == PermissionAction.EXECUTE_SHELL:
            return self._decide_shell(request, mode=mode)
        if request.action == PermissionAction.NETWORK_REQUEST:
            return self._ask("网络请求需要用户确认。")
        if request.action == PermissionAction.MCP_TOOL:
            return self._ask("MCP 工具调用需要用户确认。")
        return self._ask("未知权限请求需要用户确认。")

    def _decide_path(self, request: PermissionRequest, *, mode: PermissionMode) -> PermissionDecision:
        target = self._resolve_path(request.target, cwd=request.cwd)
        inside_root = self._is_inside_project(target)
        sensitive = self._is_sensitive_path(target)

        if request.action == PermissionAction.READ_PATH:
            if inside_root and not sensitive:
                return self._allow("允许读取项目根目录内普通路径。")
            return self._ask("读取项目根目录外或敏感路径需要用户确认。")

        if request.action == PermissionAction.WRITE_PATH:
            if not inside_root:
                return self._ask("写入项目根目录外路径需要用户确认。")
            if sensitive:
                return self._ask("写入敏感路径需要用户确认。")
            if not bool(request.metadata.get("allow_auto", True)):
                return self._ask("该写入操作需要用户确认。")
            if mode == PermissionMode.AGGRESSIVE:
                return self._allow("激进模式允许写入项目根目录内普通路径。")
            return self._ask("写入文件需要用户确认。")

        if request.action == PermissionAction.DELETE_PATH:
            if not inside_root:
                return self._deny("拒绝删除项目根目录外路径。")
            return self._ask("删除路径需要用户确认。")

        return self._ask("路径操作需要用户确认。")

    def _decide_env(self, request: PermissionRequest) -> PermissionDecision:
        key = request.target.upper()
        if any(keyword in key for keyword in _SENSITIVE_ENV_KEYWORDS):
            return self._deny("拒绝读取或展示敏感环境变量明文。")
        return self._ask("读取环境变量需要用户确认。")

    def _decide_git(self, request: PermissionRequest) -> PermissionDecision:
        command = request.target.strip()
        if _has_shell_control_operator(command):
            return self._ask("包含 shell 控制符的 git 操作需要用户确认。")
        subcommand = command.split(maxsplit=1)[0] if command else ""
        if subcommand in _READONLY_GIT_COMMANDS and self._request_cwd_inside_root(request):
            return self._allow("允许项目根目录内只读 git 操作。")
        return self._ask("git 操作需要用户确认。")

    def _decide_shell(self, request: PermissionRequest, *, mode: PermissionMode) -> PermissionDecision:
        command = request.target.strip()
        if _has_shell_control_operator(command):
            return self._ask("包含 shell 控制符的命令需要用户确认。")
        if mode == PermissionMode.AGGRESSIVE and self._request_cwd_inside_root(request):
            if _is_dangerous_shell_command(command):
                return self._ask("高风险 shell 命令需要用户确认。")
            if _is_aggressive_allowed_shell_command(command):
                return self._allow("激进模式允许项目根目录内常见验证命令。")
        return self._ask("shell 命令需要用户确认。")

    def _resolve_path(self, value: str, *, cwd: Path | None) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = (cwd or self.project_root) / path
        return path.resolve()

    def _is_inside_project(self, path: Path) -> bool:
        return path == self.project_root or self.project_root in path.parents

    def _request_cwd_inside_root(self, request: PermissionRequest) -> bool:
        cwd = (request.cwd or self.project_root).resolve()
        return cwd == self.project_root or self.project_root in cwd.parents

    def _is_sensitive_path(self, path: Path) -> bool:
        relative_parts = []
        try:
            relative_parts = list(path.relative_to(self.project_root).parts)
        except ValueError:
            relative_parts = list(path.parts)

        if ".git" in relative_parts:
            return True
        if path.name in _SENSITIVE_FILENAMES:
            return True
        return path.suffix.lower() in _SENSITIVE_SUFFIXES

    def _allow(self, reason: str) -> PermissionDecision:
        return PermissionDecision(kind=PermissionDecisionKind.ALLOW, reason=reason)

    def _ask(self, reason: str) -> PermissionDecision:
        return PermissionDecision(kind=PermissionDecisionKind.ASK, reason=reason)

    def _deny(self, reason: str) -> PermissionDecision:
        return PermissionDecision(kind=PermissionDecisionKind.DENY, reason=reason)


def _command_matches_prefix(command: str, prefix: str) -> bool:
    command = command.strip()
    prefix = prefix.strip()
    return bool(prefix) and (command == prefix or command.startswith(prefix + " "))


def _is_aggressive_allowed_shell_command(command: str) -> bool:
    if any(_command_matches_prefix(command, prefix) for prefix in _AGGRESSIVE_ALLOWED_COMMANDS):
        return True
    return _first_token(command) in _AGGRESSIVE_ALLOWED_EXECUTABLES


def _is_dangerous_shell_command(command: str) -> bool:
    return any(_command_matches_prefix(command, prefix) for prefix in _DANGEROUS_SHELL_PREFIXES)


def _first_token(command: str) -> str:
    parts = command.strip().split()
    return parts[0] if parts else ""


def _has_shell_control_operator(command: str) -> bool:
    return bool(_SHELL_CONTROL_PATTERN.search(command))
