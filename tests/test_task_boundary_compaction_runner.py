from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import tarfile

import pytest

from benchmark.task_boundary_compaction.cases import (
    load_historical_cases,
    materialize_historical_case,
)
from benchmark.task_boundary_compaction.models import Arm
from benchmark.task_boundary_compaction.runner import RunConfig, _resolve_provider, _trial_status
from firstcoder.providers.types import MainRequestOptions


BASE_COMMIT = "a" * 40
TARGET_COMMIT = "b" * 40


def _run_config(tmp_path: Path, **overrides) -> RunConfig:
    values = {
        "output_root": tmp_path / "runs",
        "run_id": "test",
        "project_root": tmp_path,
        "model": "fake/model",
        "context_window": 32_768,
    }
    values.update(overrides)
    return RunConfig(**values)


@pytest.mark.parametrize("provider_timeout_seconds", [90, 91])
def test_run_config_rejects_provider_timeout_that_leaves_no_turn_retry_time(
    tmp_path: Path,
    provider_timeout_seconds: float,
) -> None:
    with pytest.raises(ValueError, match="strictly smaller than max_turn_seconds"):
        _run_config(
            tmp_path,
            max_turn_seconds=90,
            provider_timeout_seconds=provider_timeout_seconds,
        )


def test_real_provider_resolution_uses_benchmark_output_limit_and_profile_request_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import benchmark.task_boundary_compaction.runner as runner

    profile_request = type(
        "ProfileRequest",
        (),
        {
            "temperature": 0.35,
            "max_tokens": 8_192,
            "extra_body": {"reasoning_effort": "medium"},
        },
    )()
    profile = type("Profile", (), {"request": profile_request})()
    app_config = type(
        "AppConfig",
        (),
        {"model_catalog": lambda self: type("Catalog", (), {"require": lambda self, ref: profile})()},
    )()
    provider = object()
    monkeypatch.setattr(runner, "load_config", lambda *, project_root: app_config)
    monkeypatch.setattr(
        runner,
        "create_provider_for_model",
        lambda received_config, received_profile: provider,
    )
    config = _run_config(
        tmp_path,
        model="catalog/model",
        request_options=MainRequestOptions(max_tokens=4_096),
    )

    resolved_provider, options = _resolve_provider(config)

    assert resolved_provider is provider
    assert options.temperature == 0.35
    assert options.max_tokens == 4_096
    assert options.extra_body == {"reasoning_effort": "medium"}
    assert profile_request.max_tokens == 8_192
    assert profile_request.extra_body == {"reasoning_effort": "medium"}


def _write_manifest(tmp_path: Path, *, focused_test_files: list[str] | None = None) -> Path:
    manifest = {
        "suite": "historical",
        "cases": [
            {
                "case_id": "historical-case",
                "base_commit": BASE_COMMIT,
                "target_commit": TARGET_COMMIT,
                "focused_test_files": (
                    ["tests/test_subject.py"] if focused_test_files is None else focused_test_files
                ),
            }
        ],
    }
    path = tmp_path / "historical.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _fake_git_run(*, parent: str = BASE_COMMIT, archive: bytes = b""):
    def run(command, *, cwd, check, capture_output, text=False):
        assert cwd == Path("/repo")
        assert check is True
        assert capture_output is True
        if command[1:3] == ["rev-parse", f"{TARGET_COMMIT}^"]:
            return subprocess.CompletedProcess(command, 0, stdout=parent + "\n", stderr="")
        if command[1:4] == ["log", "-1", "--format=%s"]:
            return subprocess.CompletedProcess(command, 0, stdout="Repair historical behavior\n", stderr="")
        if command[1:3] == ["archive", "--format=tar"]:
            return subprocess.CompletedProcess(command, 0, stdout=archive, stderr=b"")
        if command[1] == "show":
            return subprocess.CompletedProcess(command, 0, stdout="assert True\n" if text else b"assert True\n", stderr="")
        raise AssertionError(f"unexpected command: {command}")

    return run


def _tar_bytes(files: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for path, content in files.items():
            encoded = content.encode("utf-8")
            member = tarfile.TarInfo(path)
            member.size = len(encoded)
            archive.addfile(member, io.BytesIO(encoded))
    return output.getvalue()


def test_load_historical_cases_uses_first_parent_subject_and_stable_b_task(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "benchmark.task_boundary_compaction.cases.subprocess.run",
        _fake_git_run(),
    )

    case = load_historical_cases(_write_manifest(tmp_path), repo_root=Path("/repo"))[0]

    assert case.base_commit == BASE_COMMIT
    assert case.target_commit == TARGET_COMMIT
    assert case.commit_subject == "Repair historical behavior"
    assert case.benchmark_case.turns[0].expected_decision == "new"
    assert case.benchmark_case.turns[-1].expected_decision == "same"
    assert "Repair historical behavior" in case.benchmark_case.turns[0].message
    assert case.benchmark_case.verify_command == ("python", "-m", "pytest", "-q", "tests/test_subject.py")
    assert {case.benchmark_case.turns for _ in Arm} == {case.benchmark_case.turns}


def test_load_historical_cases_rejects_missing_focused_tests_and_non_parent_base(tmp_path, monkeypatch) -> None:
    with pytest.raises(ValueError, match="focused_test_files"):
        load_historical_cases(_write_manifest(tmp_path, focused_test_files=[]), repo_root=Path("/repo"))

    monkeypatch.setattr(
        "benchmark.task_boundary_compaction.cases.subprocess.run",
        _fake_git_run(parent="c" * 40),
    )
    with pytest.raises(ValueError, match="first parent"):
        load_historical_cases(_write_manifest(tmp_path), repo_root=Path("/repo"))


def test_materialize_historical_case_uses_fresh_base_archive_and_target_tests(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "benchmark.task_boundary_compaction.cases.subprocess.run",
        _fake_git_run(archive=_tar_bytes({"subject.py": "VALUE = 'base'\n"})),
    )
    case = load_historical_cases(_write_manifest(tmp_path), repo_root=Path("/repo"))[0]
    destination = tmp_path / "worktree"

    materialization = materialize_historical_case(
        case,
        repo_root=Path("/repo"),
        destination=destination,
    )

    assert materialization.worktree == destination
    assert (destination / "subject.py").read_text(encoding="utf-8") == "VALUE = 'base'\n"
    assert (destination / "tests/test_subject.py").read_text(encoding="utf-8") == "assert True\n"


@pytest.mark.parametrize(
    ("arm", "expected_boundary", "boundary_change_count"),
    [
        (Arm.AUTO_ONLY, True, 0),
        (Arm.CLASSIFIER_ONLY, False, 0),
        (Arm.FULL, False, 0),
    ],
)
def test_normal_auto_is_not_confounded_without_an_expected_full_boundary(
    arm: Arm,
    expected_boundary: bool,
    boundary_change_count: int,
) -> None:
    assert _trial_status(
        arm=arm,
        expected_boundary=expected_boundary,
        boundary_change_count=boundary_change_count,
        confounded_auto=True,
        provider_error=False,
        verifier_exit_code=0,
    ) == "passed"


def test_positive_full_boundary_stays_confounded_when_auto_precedes_it() -> None:
    assert _trial_status(
        arm=Arm.FULL,
        expected_boundary=True,
        boundary_change_count=1,
        confounded_auto=True,
        provider_error=False,
        verifier_exit_code=0,
    ) == "confounded_auto"


def test_positive_classifier_only_boundary_stays_confounded_when_auto_precedes_it() -> None:
    assert _trial_status(
        arm=Arm.CLASSIFIER_ONLY,
        expected_boundary=True,
        boundary_change_count=1,
        confounded_auto=True,
        provider_error=False,
        verifier_exit_code=0,
    ) == "confounded_auto"
