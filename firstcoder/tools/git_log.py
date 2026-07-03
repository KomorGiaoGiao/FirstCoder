"""`git_log` 工具。

查看 git 提交历史，补齐 git 工具链。
"""

from __future__ import annotations

from pathlib import Path

from firstcoder.permissions.types import PermissionAction
from firstcoder.tools.types import Tool, ToolPermissionSpec, ToolResult, make_error_result, make_text_result
from firstcoder.utils import git as git_utils
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.sandbox import PathSandbox
from firstcoder.utils.sandbox_access import SandboxAccess


def create_git_log_tool(root: str | Path, *, access: SandboxAccess | None = None) -> Tool:
    """创建查看 git 提交历史的工具。"""

    sandbox = PathSandbox(root, access=access)

    def git_log(path: str = ".", max_entries: int = 10) -> ToolResult:
        """查看项目提交历史；可限制路径和条数。"""

        if max_entries <= 0:
            return make_error_result("git_log", "max_entries 必须大于 0")

        try:
            target = sandbox.resolve(path)
        except ValueError as exc:
            return make_error_result("git_log", str(exc))
        relative_path = "." if target == sandbox.root else sandbox.relative(target)

        repo_result = git_utils.run_git(sandbox, ["rev-parse", "--is-inside-work-tree"])
        if repo_result.returncode != 0:
            return make_error_result("git_log", "当前目录不是 git 仓库")

        command = ["log", "--oneline", f"-n{max_entries}", "--", relative_path]
        log_result = git_utils.run_git(sandbox, command)
        if log_result.returncode != 0:
            return make_error_result("git_log", log_result.stderr.strip() or "git log 执行失败")

        lines = [line for line in log_result.stdout.strip().splitlines() if line]
        content = log_result.stdout.strip() or "没有提交历史。"

        return make_text_result(
            "git_log",
            content,
            path=relative_path,
            commits=len(lines),
            max_entries=max_entries,
        )

    tool = tool_from_function(git_log)
    tool.permission = ToolPermissionSpec(
        action=PermissionAction.GIT_OPERATION,
        target_value="log",
        reason="查看 git log 属于 git 操作。",
    )
    return tool
