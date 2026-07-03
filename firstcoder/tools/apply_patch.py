"""`apply_patch` 工具。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from firstcoder.permissions.types import PermissionAction
from firstcoder.tools.types import Tool, ToolPermissionSpec, ToolResult, make_error_result, make_text_result
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.sandbox import PathSandbox
from firstcoder.utils.sandbox_access import SandboxAccess
from firstcoder.utils.text import safe_read_text


BEGIN_MARKER = "*** Begin Patch"
END_MARKER = "*** End Patch"


@dataclass(slots=True)
class PatchHunk:
    """一次文件更新中的局部替换块。"""

    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PatchOperation:
    """解析后的单个文件操作。"""

    action: str
    path: str
    move_to: str | None = None
    add_lines: list[str] = field(default_factory=list)
    hunks: list[PatchHunk] = field(default_factory=list)


@dataclass(slots=True)
class PatchPlan:
    """完整 patch 的结构化计划。"""

    operations: list[PatchOperation]


def create_apply_patch_tool(root: str | Path, *, access: SandboxAccess | None = None) -> Tool:
    """创建多文件文本补丁工具。"""

    sandbox = PathSandbox(root, access=access)

    def apply_patch(patch: str, dry_run: bool = False) -> ToolResult:
        """按 patch 语法新增、更新、删除或移动项目内文本文件。"""

        try:
            plan = parse_patch(patch)
            outcome = _apply_plan(sandbox, plan, dry_run=dry_run)
        except ValueError as exc:
            return make_error_result("apply_patch", str(exc))

        return make_text_result(
            "apply_patch",
            "补丁可应用。" if dry_run else "补丁已应用。",
            dry_run=dry_run,
            changed_files=outcome["changed_files"],
            created_files=outcome["created_files"],
            deleted_files=outcome["deleted_files"],
            moved_files=outcome["moved_files"],
        )

    tool = tool_from_function(apply_patch)
    tool.permission = ToolPermissionSpec(
        action=PermissionAction.WRITE_PATH,
        target_builder=_permission_target_for_patch,
        reason="应用补丁会修改项目文件，需要用户确认。",
        allow_always=False,
        allow_auto=False,
    )
    return tool


def _permission_target_for_patch(arguments: dict[str, object]) -> str:
    patch = str(arguments.get("patch") or "")
    plan = parse_patch(patch)
    files: list[str] = []
    for operation in plan.operations:
        files.append(operation.path)
        if operation.move_to:
            files.append(operation.move_to)
    return ", ".join(dict.fromkeys(files))


def parse_patch(patch: str) -> PatchPlan:
    """把文本 patch 解析成文件操作列表。

    这里实现的是第一阶段可控子集：新增文件、删除文件、更新文件，以及 `@@`
    标记的上下文 hunk。后续如果要兼容更完整的 unified diff，可以在这个函数继续扩展。
    """

    lines = patch.splitlines()
    if not lines or lines[0] != BEGIN_MARKER:
        raise ValueError("patch 必须以 *** Begin Patch 开头")
    if lines[-1] != END_MARKER:
        raise ValueError("patch 必须以 *** End Patch 结尾")

    operations: list[PatchOperation] = []
    index = 1
    while index < len(lines) - 1:
        line = lines[index]
        if line.startswith("*** Add File: "):
            operation, index = _parse_add_file(lines, index)
        elif line.startswith("*** Update File: "):
            operation, index = _parse_update_file(lines, index)
        elif line.startswith("*** Delete File: "):
            operation, index = _parse_delete_file(lines, index)
        else:
            raise ValueError(f"无法识别的 patch 行：{line}")
        operations.append(operation)

    if not operations:
        raise ValueError("patch 中没有任何文件操作")
    return PatchPlan(operations=operations)


def _parse_add_file(lines: list[str], index: int) -> tuple[PatchOperation, int]:
    """解析新增文件操作。"""

    path = lines[index].removeprefix("*** Add File: ").strip()
    if not path:
        raise ValueError("Add File 缺少路径")

    add_lines: list[str] = []
    index += 1
    while index < len(lines) - 1 and not lines[index].startswith("*** "):
        line = lines[index]
        if not line.startswith("+"):
            raise ValueError("Add File 内容行必须以 + 开头")
        add_lines.append(line[1:])
        index += 1
    return PatchOperation(action="add", path=path, add_lines=add_lines), index


def _parse_update_file(lines: list[str], index: int) -> tuple[PatchOperation, int]:
    """解析更新文件操作。"""

    path = lines[index].removeprefix("*** Update File: ").strip()
    if not path:
        raise ValueError("Update File 缺少路径")

    move_to: str | None = None
    hunks: list[PatchHunk] = []
    index += 1
    while index < len(lines) - 1 and _is_update_body_line(lines[index]):
        if lines[index].startswith("*** Move to: "):
            move_to = lines[index].removeprefix("*** Move to: ").strip()
            if not move_to:
                raise ValueError("Move to 缺少路径")
            index += 1
            continue
        if not lines[index].startswith("@@"):
            raise ValueError("Update File 需要使用 @@ 开始 hunk")
        hunk, index = _parse_hunk(lines, index + 1)
        hunks.append(hunk)

    if not hunks and move_to is None:
        raise ValueError("Update File 至少需要一个 hunk")
    return PatchOperation(action="update", path=path, move_to=move_to, hunks=hunks), index


def _parse_hunk(lines: list[str], index: int) -> tuple[PatchHunk, int]:
    """解析单个更新 hunk。"""

    hunk = PatchHunk()
    while index < len(lines) - 1 and not lines[index].startswith("@@") and not lines[index].startswith("*** "):
        line = lines[index]
        if line.startswith("+"):
            hunk.new_lines.append(line[1:])
        elif line.startswith("-"):
            hunk.old_lines.append(line[1:])
        elif line.startswith(" "):
            text = line[1:]
            hunk.old_lines.append(text)
            hunk.new_lines.append(text)
        elif line == "":
            hunk.old_lines.append("")
            hunk.new_lines.append("")
        else:
            raise ValueError(f"hunk 行必须以 +、- 或空格开头：{line}")
        index += 1

    if not hunk.old_lines and not hunk.new_lines:
        raise ValueError("hunk 不能为空")
    return hunk, index


def _is_update_body_line(line: str) -> bool:
    """判断一行是否仍属于当前 Update File 操作。"""

    return not line.startswith("*** ") or line.startswith("*** Move to: ")


def _parse_delete_file(lines: list[str], index: int) -> tuple[PatchOperation, int]:
    """解析删除文件操作。"""

    path = lines[index].removeprefix("*** Delete File: ").strip()
    if not path:
        raise ValueError("Delete File 缺少路径")
    return PatchOperation(action="delete", path=path), index + 1


def _apply_plan(sandbox: PathSandbox, plan: PatchPlan, *, dry_run: bool) -> dict[str, list[str]]:
    """应用解析后的补丁计划。"""

    changed_files: list[str] = []
    created_files: list[str] = []
    deleted_files: list[str] = []
    pending_writes: list[tuple[Path, str]] = []
    pending_deletes: list[Path] = []
    moved_files: list[dict[str, str]] = []

    for operation in plan.operations:
        target = sandbox.resolve(operation.path)
        relative = sandbox.relative(target)

        if operation.action == "add":
            _plan_add_file(target, operation, pending_writes)
            created_files.append(relative)
            changed_files.append(relative)
        elif operation.action == "update":
            destination = sandbox.resolve(operation.move_to) if operation.move_to else target
            _plan_update_file(target, destination, operation, pending_writes, pending_deletes)
            destination_relative = sandbox.relative(destination)
            changed_files.append(destination_relative)
            if operation.move_to:
                moved_files.append({"source": relative, "destination": destination_relative})
        elif operation.action == "delete":
            _plan_delete_file(target, pending_deletes)
            deleted_files.append(relative)
            changed_files.append(relative)
        else:
            raise ValueError(f"未知 patch 操作：{operation.action}")

    if not dry_run:
        for target, text in pending_writes:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        for target in pending_deletes:
            target.unlink()

    return {
        "changed_files": changed_files,
        "created_files": created_files,
        "deleted_files": deleted_files,
        "moved_files": moved_files,
    }


def _plan_add_file(target: Path, operation: PatchOperation, pending_writes: list[tuple[Path, str]]) -> None:
    """准备新增文件写入。"""

    if target.exists():
        raise ValueError(f"文件已存在：{operation.path}")
    pending_writes.append((target, _join_lines(operation.add_lines)))


def _plan_update_file(
    target: Path,
    destination: Path,
    operation: PatchOperation,
    pending_writes: list[tuple[Path, str]],
    pending_deletes: list[Path],
) -> None:
    """准备更新文件写入。"""

    if not target.exists():
        raise ValueError(f"文件不存在：{operation.path}")
    if not target.is_file():
        raise ValueError(f"路径不是文件：{operation.path}")
    if destination != target and destination.exists():
        raise ValueError(f"目标文件已存在：{operation.move_to}")

    try:
        text = safe_read_text(target)
    except UnicodeDecodeError as exc:
        raise ValueError(f"文件不是 UTF-8 文本或无法作为文本读取：{operation.path}") from exc

    for hunk in operation.hunks:
        old_text = _join_lines(hunk.old_lines)
        new_text = _join_lines(hunk.new_lines)
        count = text.count(old_text)
        if count == 0:
            raise ValueError("没有找到要替换的内容")
        if count > 1:
            raise ValueError(f"匹配内容出现 {count} 次；请提供更精确的上下文")
        text = text.replace(old_text, new_text, 1)

    pending_writes.append((destination, text))
    if destination != target:
        pending_deletes.append(target)


def _plan_delete_file(target: Path, pending_deletes: list[Path]) -> None:
    """准备删除文件。"""

    if not target.exists():
        raise ValueError("文件不存在")
    if not target.is_file():
        raise ValueError("Delete File 只能删除文件")
    pending_deletes.append(target)


def _join_lines(lines: list[str]) -> str:
    """把 patch 行还原成文件文本。

    `splitlines()` 会去掉换行符，而 patch 行语义通常表示完整文本行，所以这里统一补回
    末尾换行。这个约定能让简单文本补丁保持稳定，后续如果要支持
    “No newline at EOF” 这类标记，可以在 parser 层增加显式语法。
    """

    if not lines:
        return ""
    return "\n".join(lines) + "\n"
