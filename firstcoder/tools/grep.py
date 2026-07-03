"""`grep` 工具。"""

from __future__ import annotations

import fnmatch
import shutil
from pathlib import Path

from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.execution_sandbox import ExecutionSandbox
from firstcoder.utils.sandbox_access import SandboxAccess
from firstcoder.utils.sandbox import PathSandbox
from firstcoder.utils.subprocess import run_command


DEFAULT_MAX_SEARCH_RESULTS = 50


def create_grep_tool(root: str | Path, *, access: SandboxAccess | None = None) -> Tool:
    """创建文本搜索工具。"""

    sandbox = PathSandbox(root, access=access)
    execution_sandbox = ExecutionSandbox(root, access=access)

    def grep(
        pattern: str,
        path: str = ".",
        include: str = "*",
        case_sensitive: bool = False,
        max_results: int = DEFAULT_MAX_SEARCH_RESULTS,
    ) -> ToolResult:
        """在项目内按固定字符串搜索文本；返回文件、行号和匹配行。"""

        try:
            target = sandbox.resolve_validated(path)
        except ValueError as exc:
            return make_error_result("grep", str(exc))
        if max_results <= 0:
            return make_error_result("grep", "max_results 必须大于 0")

        rg_path = shutil.which("rg")
        if rg_path:
            return _grep_with_rg(
                tool_name="grep",
                rg_path=rg_path,
                sandbox=sandbox,
                execution_sandbox=execution_sandbox,
                pattern=pattern,
                target=target,
                include=include,
                case_sensitive=case_sensitive,
                max_results=max_results,
            )

        return _grep_with_python(
            tool_name="grep",
            sandbox=sandbox,
            pattern=pattern,
            target=target,
            include=include,
            case_sensitive=case_sensitive,
            max_results=max_results,
        )

    return tool_from_function(grep)


def _grep_with_rg(
    *,
    tool_name: str,
    rg_path: str,
    sandbox: PathSandbox,
    execution_sandbox: ExecutionSandbox,
    pattern: str,
    target: Path,
    include: str,
    case_sensitive: bool,
    max_results: int,
) -> ToolResult:
    """使用 ripgrep 搜索，并把输出解析成结构化结果。"""

    command = [
        rg_path,
        "--line-number",
        "--with-filename",
        "--color", "never",
        "--fixed-strings",
        "--glob", include,
        "--max-count", str(max_results),
    ]
    if not case_sensitive:
        command.append("--ignore-case")
    command.extend([pattern, str(target)])

    result = run_command(
        command,
        cwd=sandbox.root,
        timeout_seconds=30,
        max_output_chars=1_000_000,
        env=execution_sandbox.build_env(),
    )

    if result.error:
        return _grep_with_python(
            tool_name=tool_name,
            sandbox=sandbox,
            pattern=pattern,
            target=target,
            include=include,
            case_sensitive=case_sensitive,
            max_results=max_results,
            fallback_error=result.error,
        )

    if result.exit_code == 0:
        results = _parse_rg_output(sandbox, result.stdout, max_results)
        return _format_grep_result(tool_name, results, engine="rg", truncated=len(results) >= max_results)
    if result.exit_code == 1:
        return _format_grep_result(tool_name, [], engine="rg", truncated=False)

    return _grep_with_python(
        tool_name=tool_name,
        sandbox=sandbox,
        pattern=pattern,
        target=target,
        include=include,
        case_sensitive=case_sensitive,
        max_results=max_results,
        fallback_error=result.stderr.strip(),
    )


def _grep_with_python(
    *,
    tool_name: str,
    sandbox: PathSandbox,
    pattern: str,
    target: Path,
    include: str,
    case_sensitive: bool,
    max_results: int,
    fallback_error: str | None = None,
) -> ToolResult:
    """使用 Python 实现的搜索后备路径。"""

    needle = pattern if case_sensitive else pattern.lower()
    results: list[dict[str, object]] = []
    files = [target] if target.is_file() else [item for item in target.rglob("*") if item.is_file()]

    for file_path in files:
        relative = sandbox.relative(file_path)
        if not _matches_include(file_path, relative, include):
            continue
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue

        for line_number, line in enumerate(lines, start=1):
            haystack = line if case_sensitive else line.lower()
            if needle not in haystack:
                continue
            results.append({"path": relative, "line": line_number, "text": line})
            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break

    result = _format_grep_result(tool_name, results, engine="python", truncated=len(results) >= max_results)
    if fallback_error:
        result.data["rg_error"] = fallback_error
    return result


def _parse_rg_output(sandbox: PathSandbox, output: str, max_results: int) -> list[dict[str, object]]:
    """解析 `rg --line-number --with-filename` 的输出。"""

    results: list[dict[str, object]] = []
    for line in output.splitlines():
        if len(results) >= max_results:
            break
        file_path, line_number, text = _split_rg_line(line)
        if not file_path or not line_number:
            continue
        results.append(
            {
                "path": sandbox.relative(file_path),
                "line": int(line_number),
                "text": text,
            }
        )
    return results


def _split_rg_line(line: str) -> tuple[str, str, str]:
    """按 Windows 盘符兼容方式拆分 rg 输出行。"""

    first = line.find(":")
    if first == 1 and len(line) > 2 and line[2] in ("\\", "/"):
        second = line.find(":", 3)
    else:
        second = first

    if second == -1:
        return "", "", line

    third = line.find(":", second + 1)
    if third == -1:
        return "", "", line

    return line[:second], line[second + 1 : third], line[third + 1 :]


def _format_grep_result(
    tool_name: str,
    results: list[dict[str, object]],
    *,
    engine: str,
    truncated: bool,
) -> ToolResult:
    """把搜索结果格式化为工具统一结果。"""

    content = "\n".join(f"{result['path']}:{result['line']}: {result['text']}" for result in results)
    return make_text_result(
        tool_name,
        content or "没有找到匹配内容。",
        results=results,
        engine=engine,
        truncated=truncated,
    )


def _matches_include(file_path: Path, relative: str, include: str) -> bool:
    """让 Python fallback 的 include 行为尽量贴近 ripgrep。

    简单文件名模式如 `*.py` 匹配任意目录下的 Python 文件；包含路径分隔符的模式
    则按相对路径匹配，例如 `src/*.py`。
    """

    if "/" in include or "\\" in include:
        return fnmatch.fnmatch(relative, include.replace("\\", "/"))
    return fnmatch.fnmatch(file_path.name, include)
