"""Execution sandbox for local subprocess tools."""

from __future__ import annotations

import os
import re
from pathlib import Path

from firstcoder.runtime.cancellation import current_cancellation_token
from firstcoder.utils.sandbox_access import SandboxAccess
from firstcoder.utils.sandbox import PathSandbox
from firstcoder.utils.subprocess import CommandResult, run_command

_SENSITIVE_ENV_KEYWORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ExecutionSandbox:
    """Small subprocess boundary layered above PathSandbox.

    This is intentionally not a policy engine. PermissionManager decides whether
    a command may run; this class constrains how approved subprocesses run.
    """

    def __init__(self, root: str | Path, *, access: SandboxAccess | None = None) -> None:
        self.path_sandbox = PathSandbox(root, access=access)
        self.root = self.path_sandbox.root

    def resolve_cwd(self, cwd: str | Path | None = ".") -> Path:
        return self.path_sandbox.resolve_validated(cwd, expect="dir")

    def relative(self, path: str | Path) -> str:
        return self.path_sandbox.relative(path)

    def build_env(self, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if not _is_sensitive_env_key(key)}
        for key, value in (extra_env or {}).items():
            if not _is_sensitive_env_key(key):
                env[str(key)] = str(value)
        return env

    def prepare_env_overrides(
        self,
        extra_env: dict[str, str] | None,
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        """规范化显式环境覆盖，并返回被敏感词策略拒绝的变量名。"""

        if extra_env is None:
            return {}, ()
        if not isinstance(extra_env, dict):
            raise ValueError("env 必须是字符串键值对象")
        accepted: dict[str, str] = {}
        rejected: list[str] = []
        for raw_key, raw_value in extra_env.items():
            key = str(raw_key)
            if not _ENV_KEY_RE.fullmatch(key):
                raise ValueError(f"环境变量名不合法：{key}")
            if _is_sensitive_env_key(key):
                rejected.append(key)
                continue
            if not isinstance(raw_value, str):
                raise ValueError(f"环境变量 {key} 的值必须是字符串")
            accepted[key] = raw_value
        return accepted, tuple(sorted(rejected))

    def run(
        self,
        command: list[str] | str,
        *,
        cwd: str | Path | None = ".",
        timeout_seconds: int = 30,
        max_output_chars: int = 20000,
        shell: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> CommandResult:
        try:
            workdir = self.resolve_cwd(cwd)
        except ValueError as exc:
            return CommandResult(
                exit_code=-1,
                stdout="",
                stderr="",
                stdout_truncated=False,
                stderr_truncated=False,
                ok=False,
                error=str(exc),
            )
        return run_command(
            command,
            cwd=workdir,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
            shell=shell,
            env=self.build_env(extra_env),
            cancellation_token=current_cancellation_token(),
        )


def _is_sensitive_env_key(key: str) -> bool:
    normalized = key.upper()
    return any(keyword in normalized for keyword in _SENSITIVE_ENV_KEYWORDS)
