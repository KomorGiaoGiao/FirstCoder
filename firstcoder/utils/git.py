"""项目内部复用的 git 命令辅助函数。"""

from __future__ import annotations

import subprocess

from firstcoder.utils.sandbox import PathSandbox
from firstcoder.utils.execution_sandbox import ExecutionSandbox


def run_git(sandbox: PathSandbox, args: list[str]) -> subprocess.CompletedProcess[str]:
    """在沙箱根目录执行 git 命令。"""

    execution_sandbox = ExecutionSandbox(sandbox.root)
    try:
        return subprocess.run(
            ["git", *args],
            cwd=sandbox.root,
            env=execution_sandbox.build_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(["git", *args], returncode=1, stdout="", stderr=str(exc))
