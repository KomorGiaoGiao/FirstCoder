"""Load and materialize verifier-backed benchmark cases."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
from typing import Literal

from benchmark.task_boundary_compaction.models import BenchmarkCase, TurnSpec


@dataclass(frozen=True, slots=True)
class RepositoryFile:
    """One text file materialized into a disposable controlled task project."""

    path: str
    content: str

    def __post_init__(self) -> None:
        _validate_relative_path(self.path, field_name="repository_files.path")


@dataclass(frozen=True, slots=True)
class ControlledCase:
    """A deterministic, verifier-backed fixture for the causal pilot."""

    benchmark_case: BenchmarkCase
    repository_files: tuple[RepositoryFile, ...]

    @property
    def case_id(self) -> str:
        return self.benchmark_case.case_id


@dataclass(frozen=True, slots=True)
class HistoricalCase:
    """A real FirstCoder change reconstructed from its parent snapshot."""

    benchmark_case: BenchmarkCase
    base_commit: str
    target_commit: str
    commit_subject: str
    focused_test_files: tuple[str, ...]

    @property
    def case_id(self) -> str:
        return self.benchmark_case.case_id


@dataclass(frozen=True, slots=True)
class HistoricalMaterialization:
    """Paths created for one isolated historical task trial."""

    worktree: Path
    focused_test_files: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class AiderTask:
    """One local Aider Polyglot task whose solution directory is never read."""

    task_id: str
    task_root: Path
    instruction: str
    workspace_dir: Path
    tests_dir: Path
    dockerfile: Path


@dataclass(frozen=True, slots=True)
class AiderChainCase:
    """A recorded same-task A sequence followed by independent task B."""

    benchmark_case: BenchmarkCase
    chain_type: Literal["natural", "batch"]
    a_tasks: tuple[AiderTask, ...]
    b_task: AiderTask
    a_turns: tuple[str, ...]

    @property
    def case_id(self) -> str:
        return self.benchmark_case.case_id


def load_controlled_cases(manifest_path: str | Path) -> tuple[ControlledCase, ...]:
    """Load committed deterministic cases without consulting the surrounding Git history."""

    manifest = _load_manifest(manifest_path)
    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("cases must be a non-empty list")

    cases: list[ControlledCase] = []
    seen_case_ids: set[str] = set()
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise ValueError("case entries must be objects")
        case_id = _required_string(raw_case, "case_id")
        if case_id in seen_case_ids:
            raise ValueError(f"duplicate case_id: {case_id}")
        seen_case_ids.add(case_id)
        expected_boundary = raw_case.get("expected_boundary")
        if not isinstance(expected_boundary, bool):
            raise ValueError("expected_boundary must be a boolean")
        benchmark_case = BenchmarkCase(
            case_id=case_id,
            kind="controlled",
            turns=_turns(raw_case),
            verify_command=_verify_command(raw_case),
            expected_boundary=expected_boundary,
        )
        files = _repository_files(raw_case)
        cases.append(ControlledCase(benchmark_case=benchmark_case, repository_files=files))
    return tuple(cases)


def load_historical_cases(manifest_path: str | Path, *, repo_root: str | Path) -> tuple[HistoricalCase, ...]:
    """Load manifest entries and prove each base is the target's first parent."""

    manifest = _load_manifest(manifest_path)
    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("cases must be a non-empty list")

    root = Path(repo_root)
    cases: list[HistoricalCase] = []
    seen_case_ids: set[str] = set()
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise ValueError("case entries must be objects")
        case_id = _required_string(raw_case, "case_id")
        if case_id in seen_case_ids:
            raise ValueError(f"duplicate case_id: {case_id}")
        seen_case_ids.add(case_id)
        base_commit = _required_commit(raw_case, "base_commit")
        target_commit = _required_commit(raw_case, "target_commit")
        focused_test_files = _focused_test_files(raw_case)
        first_parent = _git_text(root, ["rev-parse", f"{target_commit}^"])
        if first_parent != base_commit:
            raise ValueError(f"base_commit must be the target commit's first parent for {case_id}")
        subject = _git_text(root, ["log", "-1", "--format=%s", target_commit])
        benchmark_case = BenchmarkCase(
            case_id=case_id,
            kind="historical",
            turns=(
                TurnSpec(
                    message=(
                        f"任务 B：实现提交“{subject}”描述的改动。"
                        "请让原始聚焦测试通过，且不要编辑测试。"
                    ),
                    expected_decision="new",
                ),
                TurnSpec(
                    message="继续任务 B：运行聚焦测试，检查实现并修复剩余问题。",
                    expected_decision="same",
                ),
            ),
            verify_command=("python", "-m", "pytest", "-q", *focused_test_files),
            expected_boundary=True,
        )
        cases.append(
            HistoricalCase(
                benchmark_case=benchmark_case,
                base_commit=base_commit,
                target_commit=target_commit,
                commit_subject=subject,
                focused_test_files=focused_test_files,
            )
        )
    return tuple(cases)


def load_aider_chain_cases(
    manifest_path: str | Path,
    *,
    aider_root: str | Path,
) -> tuple[AiderChainCase, ...]:
    """Load genuine Aider task chains without reading reference solutions."""

    manifest = _load_manifest(manifest_path)
    raw_cases = manifest.get("cases")
    if manifest.get("suite") != "aider-chain":
        raise ValueError("aider chain manifest suite must be aider-chain")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("cases must be a non-empty list")

    root = Path(aider_root).resolve()
    if not root.is_dir():
        raise ValueError(f"Aider root does not exist: {root}")
    cases: list[AiderChainCase] = []
    case_ids: set[str] = set()
    used_task_ids: set[str] = set()
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise ValueError("case entries must be objects")
        case_id = _required_string(raw_case, "case_id")
        if case_id in case_ids:
            raise ValueError(f"duplicate case_id: {case_id}")
        case_ids.add(case_id)
        chain_type = _required_string(raw_case, "chain_type")
        if chain_type not in {"natural", "batch"}:
            raise ValueError("chain_type must be natural or batch")
        a_task_ids = _aider_task_ids(raw_case, field_name="a_task_ids")
        if chain_type == "natural" and len(a_task_ids) != 1:
            raise ValueError("natural Aider chains must contain exactly one task A")
        if chain_type == "batch" and len(a_task_ids) != 3:
            raise ValueError("batch Aider chains must contain exactly three task A items")
        b_task_id = _aider_task_id(_required_string(raw_case, "b_task_id"), field_name="b_task_id")
        all_task_ids = (*a_task_ids, b_task_id)
        if len(set(all_task_ids)) != len(all_task_ids):
            raise ValueError("task B must not overlap task A")
        duplicate = next((task_id for task_id in all_task_ids if task_id in used_task_ids), None)
        if duplicate is not None:
            raise ValueError(f"reused Aider task: {duplicate}")
        used_task_ids.update(all_task_ids)
        a_tasks = tuple(_load_aider_task(root, task_id) for task_id in a_task_ids)
        b_task = _load_aider_task(root, b_task_id)
        benchmark_case = BenchmarkCase(
            case_id=case_id,
            kind="aider_chain",
            turns=(
                TurnSpec(
                    message=(
                        "任务 B：这是与先前任务无关的新 Java 练习。请在当前工作区解决它，"
                        "不要改测试或安装额外依赖。\n\n"
                        f"{b_task.instruction}"
                    ),
                    expected_decision="new",
                ),
                TurnSpec(
                    message="继续任务 B：运行该题提供的验证命令，检查并修复剩余问题。",
                    expected_decision="same",
                ),
            ),
            verify_command=("__aider_docker_verifier__",),
            expected_boundary=True,
        )
        cases.append(
            AiderChainCase(
                benchmark_case=benchmark_case,
                chain_type=chain_type,
                a_tasks=a_tasks,
                b_task=b_task,
                a_turns=_aider_a_turns(a_tasks),
            )
        )
    return tuple(cases)


def materialize_aider_task(task: AiderTask, *, destination: str | Path) -> Path:
    """Copy only an exercise workspace into a disposable agent project."""

    target = Path(destination)
    _copy_aider_workspace(task, destination=target)
    _initialize_disposable_git_repository(target)
    return target


def materialize_aider_batch(
    chain: AiderChainCase,
    *,
    destination: str | Path,
) -> tuple[tuple[str, Path], ...]:
    """Place each A exercise in a named child directory of one batch project."""

    root = Path(destination)
    if root.exists():
        raise FileExistsError(f"Aider batch destination already exists: {root}")
    root.mkdir(parents=True)
    materialized: list[tuple[str, Path]] = []
    for index, task in enumerate(chain.a_tasks, start=1):
        task_root = root / f"{index:02d}-{task.task_id}"
        _copy_aider_workspace(task, destination=task_root)
        materialized.append((task.task_id, task_root))
    _initialize_disposable_git_repository(root)
    return tuple(materialized)


def materialize_historical_case(
    case: HistoricalCase,
    *,
    repo_root: str | Path,
    destination: str | Path,
) -> HistoricalMaterialization:
    """Create a fresh base snapshot and overwrite only its target-version focused tests."""

    root = Path(repo_root)
    worktree = Path(destination)
    if worktree.exists():
        raise FileExistsError(f"historical worktree already exists: {worktree}")
    worktree.mkdir(parents=True)

    archive = _git_bytes(root, ["archive", "--format=tar", case.base_commit])
    _extract_archive(archive, destination=worktree)
    materialized_tests: list[Path] = []
    for test_path in case.focused_test_files:
        content = _git_text(root, ["show", f"{case.target_commit}:{test_path}"])
        destination_path = _safe_child_path(worktree, test_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_text(content + ("" if content.endswith("\n") else "\n"), encoding="utf-8")
        materialized_tests.append(destination_path)
    return HistoricalMaterialization(worktree=worktree, focused_test_files=tuple(materialized_tests))


def materialize_controlled_case(case: ControlledCase, *, destination: str | Path) -> Path:
    """Write the case's fixed files into a fresh disposable project directory."""

    worktree = Path(destination)
    if worktree.exists():
        raise FileExistsError(f"controlled worktree already exists: {worktree}")
    worktree.mkdir(parents=True)
    for repository_file in case.repository_files:
        destination_path = _safe_child_path(worktree, repository_file.path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_text(repository_file.content, encoding="utf-8")
    return worktree


def _load_aider_task(root: Path, task_id: str) -> AiderTask:
    task_root = (root / task_id).resolve()
    if not task_root.is_relative_to(root):
        raise ValueError(f"Aider task escapes root: {task_id}")
    instruction_path = task_root / "instruction.md"
    workspace_dir = task_root / "environment" / "workspace"
    tests_dir = task_root / "tests"
    dockerfile = task_root / "environment" / "Dockerfile"
    required_paths = (instruction_path, workspace_dir, tests_dir, dockerfile)
    if not task_root.is_dir() or not all(path.exists() for path in required_paths):
        raise ValueError(f"invalid Aider task layout: {task_id}")
    instruction = instruction_path.read_text(encoding="utf-8").strip()
    if not instruction:
        raise ValueError(f"Aider instruction must not be blank: {task_id}")
    return AiderTask(
        task_id=task_id,
        task_root=task_root,
        instruction=instruction,
        workspace_dir=workspace_dir,
        tests_dir=tests_dir,
        dockerfile=dockerfile,
    )


def _copy_aider_workspace(task: AiderTask, *, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"Aider destination already exists: {destination}")
    shutil.copytree(task.workspace_dir, destination)
    gradlew = destination / "gradlew"
    if gradlew.is_file():
        gradlew.chmod(gradlew.stat().st_mode | 0o111)


def _initialize_disposable_git_repository(project_root: Path) -> None:
    """Prevent temporary exercises from walking upward into the FirstCoder Git tree."""

    subprocess.run(["git", "init", "--quiet"], cwd=project_root, check=True)
    subprocess.run(["git", "add", "--all"], cwd=project_root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=FirstCoder Benchmark",
            "-c",
            "user.email=benchmark@localhost",
            "commit",
            "--quiet",
            "--message=benchmark baseline",
        ],
        cwd=project_root,
        check=True,
    )


def _aider_a_turns(tasks: tuple[AiderTask, ...]) -> tuple[str, ...]:
    turns: list[str] = []
    for index, task in enumerate(tasks, start=1):
        relative_dir = f"{index:02d}-{task.task_id}"
        turns.extend(
            (
                (
                    "任务 A：完成这份 Java 练习修复交付中的当前项目。现在只分析代码、题面和"
                    f"可能的验证路径，不要修改文件。项目目录：{relative_dir}\n\n{task.instruction}"
                ),
                (
                    f"继续任务 A：根据刚才的分析，在 {relative_dir} 实现当前练习。"
                    "不要编辑测试，也不要安装额外依赖。"
                ),
                (
                    f"继续任务 A：在 {relative_dir} 运行当前练习的验证，检查并修复剩余问题。"
                ),
            )
        )
    return tuple(turns)


def _aider_task_ids(data: dict[str, object], *, field_name: str) -> tuple[str, ...]:
    raw_ids = data.get(field_name)
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError(f"{field_name} must be a non-empty list")
    task_ids = tuple(
        _aider_task_id(raw_task_id, field_name=field_name)
        for raw_task_id in raw_ids
        if isinstance(raw_task_id, str)
    )
    if len(task_ids) != len(raw_ids) or len(set(task_ids)) != len(task_ids):
        raise ValueError(f"{field_name} must contain unique non-blank task ids")
    return task_ids


def _aider_task_id(value: str, *, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-blank task id")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 1 or str(path) in {"", "."}:
        raise ValueError(f"{field_name} must be a safe Aider task id")
    return str(path)


def _load_manifest(path: str | Path) -> dict[str, object]:
    try:
        parsed = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid historical manifest JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("historical manifest must be an object")
    return parsed


def _required_string(data: dict[str, object], field_name: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-blank string")
    return value


def _required_commit(data: dict[str, object], field_name: str) -> str:
    value = _required_string(data, field_name)
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value.lower()):
        raise ValueError(f"{field_name} must be a full 40-character Git commit")
    return value


def _focused_test_files(data: dict[str, object]) -> tuple[str, ...]:
    raw_paths = data.get("focused_test_files")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValueError("focused_test_files must contain at least one test path")
    paths: list[str] = []
    for raw_path in raw_paths:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("focused_test_files must contain non-blank strings")
        _validate_relative_path(raw_path, field_name="focused_test_files")
        path = PurePosixPath(raw_path)
        if not str(path).startswith("tests/"):
            raise ValueError("focused_test_files must stay below tests/")
        paths.append(str(path))
    return tuple(paths)


def _turns(data: dict[str, object]) -> tuple[TurnSpec, ...]:
    raw_turns = data.get("turns")
    if not isinstance(raw_turns, list):
        raise ValueError("turns must be a list")
    turns: list[TurnSpec] = []
    for raw_turn in raw_turns:
        if not isinstance(raw_turn, dict):
            raise ValueError("turns must contain objects")
        message = _required_string(raw_turn, "message")
        expected_decision = _required_string(raw_turn, "expected_decision")
        turns.append(TurnSpec(message=message, expected_decision=expected_decision))
    return tuple(turns)


def _verify_command(data: dict[str, object]) -> tuple[str, ...]:
    raw_command = data.get("verify_command")
    if not isinstance(raw_command, list) or not raw_command:
        raise ValueError("verify_command must be a non-empty list")
    if any(not isinstance(part, str) or not part.strip() for part in raw_command):
        raise ValueError("verify_command must contain non-blank strings")
    return tuple(raw_command)


def _repository_files(data: dict[str, object]) -> tuple[RepositoryFile, ...]:
    raw_files = data.get("repository_files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("repository_files must be a non-empty list")
    files: list[RepositoryFile] = []
    paths: set[str] = set()
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise ValueError("repository_files must contain objects")
        path = _required_string(raw_file, "path")
        content = raw_file.get("content")
        if not isinstance(content, str):
            raise ValueError("repository_files.content must be a string")
        if path in paths:
            raise ValueError(f"duplicate repository file path: {path}")
        paths.add(path)
        files.append(RepositoryFile(path=path, content=content))
    return tuple(files)


def _git_text(repo_root: Path, arguments: list[str]) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _git_bytes(repo_root: Path, arguments: list[str]) -> bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    return completed.stdout


def _extract_archive(archive_bytes: bytes, *, destination: Path) -> None:
    destination_root = destination.resolve()
    with tarfile.open(fileobj=BytesIO(archive_bytes), mode="r") as archive:
        members = archive.getmembers()
        for member in members:
            _safe_child_path(destination_root, member.name)
        archive.extractall(destination_root, members=members, filter="data")


def _safe_child_path(root: Path, relative_path: str) -> Path:
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes benchmark worktree: {relative_path}")
    return candidate


def _validate_relative_path(value: str, *, field_name: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
        raise ValueError(f"{field_name} must be a safe relative path")
