"""Trusted pre-write previews for direct local file mutation tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from difflib import unified_diff
from hashlib import sha256
from pathlib import Path
from typing import Literal

from firstcoder.providers.types import ToolCall
from firstcoder.tools.apply_patch import PatchPlan, _apply_plan, parse_patch
from firstcoder.utils.sandbox import PathSandbox
from firstcoder.utils.sandbox_access import SandboxAccess
from firstcoder.utils.text import safe_read_text

ReviewOperation = Literal["create", "modify", "delete", "move", "delete_directory", "unchanged"]


@dataclass(frozen=True, slots=True)
class ReviewFile:
    path: str
    operation: ReviewOperation
    before_digest: str | None
    after_digest: str | None
    diff: str
    added_lines: int
    removed_lines: int
    source_path: str | None = None
    binary: bool = False
    snapshot: tuple[tuple[str, str | None], ...] = ()

    def to_payload(self) -> dict[str, object]:
        data = asdict(self)
        data.pop("snapshot", None)
        return data


@dataclass(frozen=True, slots=True)
class ReviewSummary:
    created_files: int = 0
    modified_files: int = 0
    deleted_files: int = 0
    moved_files: int = 0
    deleted_directories: int = 0
    unchanged_files: int = 0
    added_lines: int = 0
    removed_lines: int = 0

    def to_payload(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PrewriteReview:
    tool_name: str
    files: tuple[ReviewFile, ...] = ()
    summary: ReviewSummary = field(default_factory=ReviewSummary)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_payload(self) -> dict[str, object]:
        return {
            "tool_name": self.tool_name,
            "files": [item.to_payload() for item in self.files],
            "summary": self.summary.to_payload(),
            "error": self.error,
        }

    def is_current(self, root: str | Path, *, access: SandboxAccess | None = None) -> bool:
        sandbox = PathSandbox(root, access=access)
        for review_file in self.files:
            for path, expected_digest in review_file.snapshot:
                target = _snapshot_path(sandbox, path)
                actual_digest = _path_digest(target)
                if actual_digest != expected_digest:
                    return False
        return True


def supports_prewrite_review(tool_name: str) -> bool:
    return tool_name in {"write", "edit", "apply_patch", "delete"}


def build_prewrite_review(
    root: str | Path,
    tool_call: ToolCall,
    *,
    access: SandboxAccess | None = None,
) -> PrewriteReview:
    if not supports_prewrite_review(tool_call.name):
        return PrewriteReview(tool_name=tool_call.name, error=f"工具 {tool_call.name} 不支持写前预览")

    sandbox = PathSandbox(root, access=access)
    try:
        if tool_call.name == "write":
            files = (_review_write(sandbox, tool_call.arguments),)
        elif tool_call.name == "edit":
            files = (_review_edit(sandbox, tool_call.arguments),)
        elif tool_call.name == "apply_patch":
            files = tuple(_review_apply_patch(sandbox, tool_call.arguments))
        else:
            files = tuple(_review_delete(sandbox, tool_call.arguments))
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        return PrewriteReview(tool_name=tool_call.name, error=str(exc))
    return PrewriteReview(tool_name=tool_call.name, files=files, summary=_summarize(files))


def _review_write(sandbox: PathSandbox, arguments: dict[str, object]) -> ReviewFile:
    path = _required_string(arguments, "path")
    content = _required_string(arguments, "content")
    create_dirs = bool(arguments.get("create_dirs", True))
    overwrite = bool(arguments.get("overwrite", True))
    target = sandbox.resolve(path)
    if target.exists() and target.is_dir():
        raise ValueError(f"路径是目录，不能写入文件：{path}")
    if target.exists() and not overwrite:
        raise ValueError(f"文件已存在且 overwrite 为 False：{path}")
    if not target.parent.exists() and not create_dirs:
        raise ValueError(f"父目录不存在：{sandbox.relative(target.parent)}")
    before = _read_existing_text(target)
    operation: ReviewOperation = "create" if before is None else "modify"
    if before == content:
        operation = "unchanged"
    return _review_file(
        path=sandbox.relative(target),
        operation=operation,
        before=before,
        after=content,
    )


def _review_edit(sandbox: PathSandbox, arguments: dict[str, object]) -> ReviewFile:
    path = _required_string(arguments, "path")
    old = _required_string(arguments, "old")
    new = _required_string(arguments, "new")
    replace_all = bool(arguments.get("replace_all", False))
    if not old:
        raise ValueError("old 不能为空")
    target = sandbox.resolve_validated(path, expect="file")
    before = safe_read_text(target)
    count = before.count(old)
    if count == 0:
        raise ValueError("没有找到匹配内容")
    if count > 1 and not replace_all:
        raise ValueError(f"匹配内容出现 {count} 次；请提供更精确的 old，或启用 replace_all")
    after = before.replace(old, new) if replace_all else before.replace(old, new, 1)
    return _review_file(
        path=sandbox.relative(target),
        operation="modify" if before != after else "unchanged",
        before=before,
        after=after,
    )


def _review_apply_patch(sandbox: PathSandbox, arguments: dict[str, object]) -> list[ReviewFile]:
    patch = _required_string(arguments, "patch")
    plan = parse_patch(patch)
    _validate_unique_patch_paths(sandbox, plan)
    before = _patch_path_states(sandbox, plan)
    _apply_plan(sandbox, plan, dry_run=True)
    after = _project_patch_states(sandbox, plan)
    reviews: list[ReviewFile] = []
    for source_path, destination_path, operation in _patch_review_targets(sandbox, plan):
        source_before = before.get(source_path)
        destination_before = before.get(destination_path)
        destination_after = after.get(destination_path)
        reviews.append(
            _review_file(
                path=destination_path,
                operation=operation,
                before=source_before if operation == "move" else destination_before,
                after=destination_after,
                source_path=source_path if operation == "move" else None,
                snapshot=_snapshot_for_paths(
                    sandbox,
                    [source_path, destination_path] if operation == "move" else [destination_path],
                ),
            )
        )
    return reviews


def _validate_unique_patch_paths(sandbox: PathSandbox, plan: PatchPlan) -> None:
    seen: set[str] = set()
    for operation in plan.operations:
        paths = [sandbox.relative(sandbox.resolve(operation.path))]
        if operation.move_to:
            paths.append(sandbox.relative(sandbox.resolve(operation.move_to)))
        for path in paths:
            if path in seen:
                raise ValueError(f"patch 不能重复修改同一路径：{path}")
            seen.add(path)


def _review_delete(sandbox: PathSandbox, arguments: dict[str, object]) -> list[ReviewFile]:
    path = _required_string(arguments, "path")
    recursive = bool(arguments.get("recursive", False))
    lexical = sandbox.root / Path(path)
    target = sandbox.resolve(path)
    if target == sandbox.root:
        raise ValueError("不能删除项目根目录")
    if not lexical.exists() and not lexical.is_symlink():
        raise ValueError(f"路径不存在：{path}")
    if lexical.is_symlink():
        parent = lexical.parent.resolve()
        if not sandbox.access.unrestricted and parent != sandbox.root and sandbox.root not in parent.parents:
            raise ValueError(f"路径超出项目目录：{path}")
        relative = lexical.relative_to(sandbox.root).as_posix()
    else:
        relative = sandbox.relative(lexical)
    if lexical.is_dir() and not lexical.is_symlink():
        if not recursive:
            raise ValueError("删除目录必须启用 recursive")
        files = [item for item in sorted(lexical.rglob("*")) if item.is_file() or item.is_symlink()]
        reviews = [
            _review_file(
                path=item.relative_to(sandbox.root).as_posix(),
                operation="delete",
                before=(f"Symbolic link will be deleted: {item.readlink()}" if item.is_symlink() else _read_existing_text_or_binary(item)),
                after=None,
                snapshot=(
                    (
                        item.relative_to(sandbox.root).as_posix(),
                        _path_digest(item),
                    ),
                ),
            )
            for item in files
        ]
        reviews.insert(
            0,
            ReviewFile(
                path=relative,
                operation="delete_directory",
                before_digest=_path_digest(lexical),
                after_digest=None,
                diff="",
                added_lines=0,
                removed_lines=0,
                snapshot=((relative, _path_digest(lexical)),),
            ),
        )
        return reviews
    return [
        _review_file(
            path=relative,
            operation="delete",
            before=(f"Symbolic link will be deleted: {lexical.readlink()}" if lexical.is_symlink() else _read_existing_text_or_binary(lexical)),
            after=None,
            snapshot=((relative, _path_digest(lexical)),),
        )
    ]


def _patch_path_states(sandbox: PathSandbox, plan: PatchPlan) -> dict[str, str | None]:
    paths: set[str] = set()
    for operation in plan.operations:
        paths.add(sandbox.relative(sandbox.resolve(operation.path)))
        if operation.move_to:
            paths.add(sandbox.relative(sandbox.resolve(operation.move_to)))
    return {path: _read_existing_text(sandbox.resolve(path)) for path in paths}


def _project_patch_states(sandbox: PathSandbox, plan: PatchPlan) -> dict[str, str | None]:
    states = _patch_path_states(sandbox, plan)
    for operation in plan.operations:
        source = sandbox.relative(sandbox.resolve(operation.path))
        destination = sandbox.relative(sandbox.resolve(operation.move_to)) if operation.move_to else source
        if operation.action == "add":
            states[destination] = "\n".join(operation.add_lines) + ("\n" if operation.add_lines else "")
            continue
        if operation.action == "delete":
            states[source] = None
            continue
        text = states[source]
        if text is None:
            raise ValueError(f"文件不存在：{operation.path}")
        for hunk in operation.hunks:
            old_text = "\n".join(hunk.old_lines) + ("\n" if hunk.old_lines else "")
            new_text = "\n".join(hunk.new_lines) + ("\n" if hunk.new_lines else "")
            text = text.replace(old_text, new_text, 1)
        states[destination] = text
        if destination != source:
            states[source] = None
    return states


def _patch_review_targets(
    sandbox: PathSandbox,
    plan: PatchPlan,
) -> list[tuple[str, str, ReviewOperation]]:
    targets: list[tuple[str, str, ReviewOperation]] = []
    for operation in plan.operations:
        source = sandbox.relative(sandbox.resolve(operation.path))
        destination = sandbox.relative(sandbox.resolve(operation.move_to)) if operation.move_to else source
        if operation.action == "add":
            targets.append((source, destination, "create"))
        elif operation.action == "delete":
            targets.append((source, source, "delete"))
        elif operation.move_to:
            targets.append((source, destination, "move"))
        else:
            targets.append((source, destination, "modify"))
    return targets


def _review_file(
    *,
    path: str,
    operation: ReviewOperation,
    before: str | bytes | None,
    after: str | None,
    source_path: str | None = None,
    snapshot: tuple[tuple[str, str | None], ...] | None = None,
) -> ReviewFile:
    binary = isinstance(before, bytes) or isinstance(after, bytes)
    if binary:
        diff = "Binary file will be deleted" if after is None else "Binary file change"
        added_lines = 0
        removed_lines = 0
    else:
        diff, added_lines, removed_lines = _unified_diff(path, before, after, source_path=source_path)
    snapshot = snapshot or ((path, _content_digest(before)),)
    return ReviewFile(
        path=path,
        operation=operation,
        before_digest=_content_digest(before),
        after_digest=_content_digest(after),
        diff=diff,
        added_lines=added_lines,
        removed_lines=removed_lines,
        source_path=source_path,
        binary=binary,
        snapshot=snapshot,
    )


def _unified_diff(
    path: str,
    before: str | None,
    after: str | None,
    *,
    source_path: str | None,
) -> tuple[str, int, int]:
    from_file = source_path or path
    before_lines = (before or "").splitlines()
    after_lines = (after or "").splitlines()
    lines = list(
        unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{from_file}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    added_lines = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed_lines = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return "\n".join(lines), added_lines, removed_lines


def _summarize(files: tuple[ReviewFile, ...]) -> ReviewSummary:
    return ReviewSummary(
        created_files=sum(item.operation == "create" for item in files),
        modified_files=sum(item.operation == "modify" for item in files),
        deleted_files=sum(item.operation == "delete" for item in files),
        moved_files=sum(item.operation == "move" for item in files),
        deleted_directories=sum(item.operation == "delete_directory" for item in files),
        unchanged_files=sum(item.operation == "unchanged" for item in files),
        added_lines=sum(item.added_lines for item in files),
        removed_lines=sum(item.removed_lines for item in files),
    )


def _required_string(arguments: dict[str, object], name: str) -> str:
    if name not in arguments:
        raise ValueError(f"缺少参数：{name}")
    value = arguments[name]
    if not isinstance(value, str):
        raise ValueError(f"参数 {name} 必须是字符串")
    return value


def _read_existing_text(path: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_dir() and not path.is_symlink():
        raise ValueError(f"路径不是文件：{path}")
    return safe_read_text(path)


def _snapshot_for_paths(sandbox: PathSandbox, paths: list[str]) -> tuple[tuple[str, str | None], ...]:
    return tuple((path, _path_digest(_snapshot_path(sandbox, path))) for path in dict.fromkeys(paths))


def _snapshot_path(sandbox: PathSandbox, path: str) -> Path:
    lexical = sandbox.root / Path(path)
    if lexical.is_symlink():
        parent = lexical.parent.resolve()
        if not sandbox.access.unrestricted and parent != sandbox.root and sandbox.root not in parent.parents:
            raise ValueError(f"路径超出项目目录：{path}")
        return lexical
    return sandbox.resolve(path)


def _content_digest(content: str | bytes | None) -> str | None:
    if content is None:
        return None
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    return sha256(data).hexdigest()


def _path_digest(path: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        return sha256(f"symlink:{path.readlink()}".encode("utf-8")).hexdigest()
    if path.is_file():
        return sha256(path.read_bytes()).hexdigest()
    digest = sha256()
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if item.is_symlink():
            digest.update(f"symlink:{item.readlink()}".encode("utf-8"))
        elif item.is_file():
            digest.update(sha256(item.read_bytes()).hexdigest().encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _read_existing_text_or_binary(path: Path) -> str | bytes | None:
    try:
        return _read_existing_text(path)
    except UnicodeDecodeError:
        return path.read_bytes()
