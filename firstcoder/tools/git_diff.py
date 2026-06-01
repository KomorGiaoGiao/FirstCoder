"""`git_diff` 工具。"""

from __future__ import annotations

from pathlib import Path

from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils import git as git_utils
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.sandbox import PathSandbox
from firstcoder.utils.text import truncate


def create_git_diff_tool(root: str | Path) -> Tool:
    """创建查看 git diff 的工具。"""

    sandbox = PathSandbox(root)

    def git_diff(path: str = ".", staged: bool = False, max_chars: int = 20000) -> ToolResult:
        """查看当前项目 git diff。"""

        if max_chars <= 0:
            return make_error_result("git_diff", "max_chars 必须大于 0")

        target = sandbox.resolve(path)
        relative_path = "." if target == sandbox.root else sandbox.relative(target)

        repo_result = git_utils.run_git(sandbox, ["rev-parse", "--is-inside-work-tree"])
        if repo_result.returncode != 0:
            return make_error_result("git_diff", "当前目录不是 git 仓库")

        command = ["diff"]
        if staged:
            command.append("--cached")
        command.extend(["--", relative_path])

        diff_result = git_utils.run_git(sandbox, command)
        if diff_result.returncode not in (0, 1):
            return make_error_result("git_diff", diff_result.stderr.strip() or "git diff 执行失败")

        diff_text = diff_result.stdout
        content, truncated = truncate(diff_text, max_chars, suffix="\n\n[diff 已截断]")

        return make_text_result(
            "git_diff",
            content or "没有 diff。",
            path=relative_path,
            staged=staged,
            truncated=truncated,
        )

    return tool_from_function(git_diff)
