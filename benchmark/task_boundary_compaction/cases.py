"""Load and materialize verifier-backed benchmark cases."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile

from benchmark.task_boundary_compaction.models import BenchmarkCase, TurnSpec


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
        path = PurePosixPath(raw_path)
        if path.is_absolute() or ".." in path.parts or not str(path).startswith("tests/"):
            raise ValueError("focused_test_files must stay below tests/")
        paths.append(str(path))
    return tuple(paths)


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
