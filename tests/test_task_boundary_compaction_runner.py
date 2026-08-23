from __future__ import annotations

import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
from dataclasses import dataclass, field

import pytest

from benchmark.task_boundary_compaction.cases import (
    AiderChainCase,
    AiderTask,
    load_historical_cases,
    materialize_historical_case,
)
from benchmark.task_boundary_compaction.models import Arm, BenchmarkCase, ProviderCallMetric, TrialResult, TurnSpec
from benchmark.task_boundary_compaction.runner import (
    RunConfig,
    _aider_docker_command,
    _resolve_providers,
    _trial_status,
    run_aider_chain_case,
)
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse, TokenUsage
from firstcoder.providers.types import MainRequestOptions


BASE_COMMIT = "a" * 40
TARGET_COMMIT = "b" * 40


@dataclass
class _ChainProvider(ChatProvider):
    main_requests: list[ChatRequest] = field(default_factory=list)
    classifier_requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        usage = TokenUsage(input_tokens=10, output_tokens=2, total_tokens=12)
        if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
            self.classifier_requests.append(request)
            basis_message = next(
                (
                    message
                    for message in reversed(request.messages)
                    if re.search(r"basis_message_id=([A-Za-z0-9_]+)", message.content)
                ),
                None,
            )
            assert basis_message is not None
            decision = "new" if not self.main_requests else "same"
            basis_match = re.search(r"basis_message_id=([A-Za-z0-9_]+)", basis_message.content)
            assert basis_match is not None
            basis_message_id = basis_match.group(1)
            return ChatResponse(
                provider=self.name,
                model=self.model,
                content=f'{{"decision":"{decision}","basis_message_id":"{basis_message_id}"}}',
                usage=usage,
            )
        self.main_requests.append(request)
        return ChatResponse(provider=self.name, model=self.model, content="完成", usage=usage)


def _aider_task(tmp_path: Path, task_id: str) -> AiderTask:
    task_root = tmp_path / task_id
    workspace = task_root / "environment" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "Subject.java").write_text("class Subject {}\n", encoding="utf-8")
    tests = task_root / "tests"
    tests.mkdir()
    (tests / "test.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dockerfile = task_root / "environment" / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    return AiderTask(
        task_id=task_id,
        task_root=task_root,
        instruction=f"# Instructions\n\nImplement {task_id}.",
        workspace_dir=workspace,
        tests_dir=tests,
        dockerfile=dockerfile,
    )


def test_aider_docker_command_mounts_disposable_project_and_readonly_original_tests(tmp_path: Path) -> None:
    task_root = tmp_path / "polyglot_java_zipper"
    workspace = task_root / "environment" / "workspace"
    workspace.mkdir(parents=True)
    tests = task_root / "tests"
    tests.mkdir()
    dockerfile = task_root / "environment" / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    task = AiderTask(
        task_id="polyglot_java_zipper",
        task_root=task_root,
        instruction="# Instructions\n",
        workspace_dir=workspace,
        tests_dir=tests,
        dockerfile=dockerfile,
    )
    project = tmp_path / "trial" / "project"
    project.mkdir(parents=True)

    verifier_logs = tmp_path / "trial" / "verifier-logs"
    build, run = _aider_docker_command(
        task=task,
        project_root=project,
        verifier_log_dir=verifier_logs,
        image_tag="firstcoder-tbc-a1b2",
    )

    assert build == ["docker", "build", "--tag", "firstcoder-tbc-a1b2", str(task_root / "environment")]
    assert run[:4] == ["docker", "run", "--rm", "--mount"]
    assert any(f"source={project.resolve()},target=/app" in argument for argument in run)
    assert any(f"source={tests.resolve()},target=/tests,readonly" in argument for argument in run)
    assert any(f"source={verifier_logs.resolve()},target=/logs/verifier" in argument for argument in run)
    assert run[-3:-1] == ["bash", "-c"]
    assert "reward.txt" in run[-1]


def test_trial_result_round_trips_fixed_task_a_metrics() -> None:
    result = TrialResult(
        case_id="chain",
        arm=Arm.FULL,
        model="main",
        classifier_model="classifier",
        context_window=32_768,
        status="passed",
        verifier_exit_code=0,
        verifier_stdout_sha256="stdout",
        verifier_stderr_sha256="stderr",
        recorded_task_a_calls=(
            ProviderCallMetric("main", input_tokens=10, output_tokens=5, total_tokens=15, elapsed_seconds=0.1),
        ),
    )

    restored = TrialResult.from_dict(result.to_dict())

    assert restored.recorded_task_a_calls == result.recorded_task_a_calls


def test_aider_chain_records_task_a_once_then_replays_it_for_all_b_arms(tmp_path: Path, monkeypatch) -> None:
    import benchmark.task_boundary_compaction.runner as runner

    task_a = _aider_task(tmp_path, "task-a")
    task_b = _aider_task(tmp_path, "task-b")
    chain = AiderChainCase(
        benchmark_case=BenchmarkCase(
            case_id="a-to-b",
            kind="aider_chain",
            turns=(
                TurnSpec("任务 B：解决独立题", "new"),
                TurnSpec("继续任务 B：验证", "same"),
            ),
            verify_command=("__aider_docker_verifier__",),
            expected_boundary=True,
        ),
        chain_type="natural",
        a_tasks=(task_a,),
        b_task=task_b,
        a_turns=("任务 A：只分析 task-a", "继续任务 A：实现 task-a", "继续任务 A：验证 task-a"),
    )
    providers: list[_ChainProvider] = []

    def provider_factory() -> _ChainProvider:
        provider = _ChainProvider()
        providers.append(provider)
        return provider

    monkeypatch.setattr(runner, "_run_aider_verifier", lambda **_kwargs: (0, "ok", ""))
    results = run_aider_chain_case(
        chain,
        arms=tuple(Arm),
        config=_run_config(
            tmp_path,
            provider_factory=provider_factory,
            max_turn_seconds=90,
            provider_timeout_seconds=45,
        ),
    )

    assert len(results) == 3
    assert all(len(result.recorded_task_a_calls) >= 3 for result in results)
    assert all(len(result.provider_calls) >= 2 for result in results)
    assert all(result.verifier_exit_code == 0 for result in results)
    assert {result.arm: result.status for result in results} == {
        Arm.AUTO_ONLY: "passed",
        Arm.CLASSIFIER_ONLY: "passed",
        Arm.FULL: "passed",
    }
    assert next(result for result in results if result.arm is Arm.FULL).task_hash_changed_count == 1
    assert len({result.recorded_task_a_calls for result in results}) == 1
    assert not (tmp_path / "runs" / "test" / "a-to-b" / "capture" / "project").exists()


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


def test_real_provider_resolution_uses_distinct_classifier_profile_when_configured(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import benchmark.task_boundary_compaction.runner as runner

    terra_request = type(
        "ProfileRequest",
        (),
        {
            "temperature": 0.35,
            "max_tokens": 8_192,
            "extra_body": {"reasoning_effort": "medium"},
        },
    )()
    luna_request = type(
        "ProfileRequest",
        (),
        {
            "temperature": 0.0,
            "max_tokens": 1_024,
            "extra_body": {"reasoning_effort": "low"},
        },
    )()
    terra_profile = type("Profile", (), {"request": terra_request})()
    luna_profile = type("Profile", (), {"request": luna_request})()
    profiles = {
        "Yuren/gpt-5.6-terra": terra_profile,
        "Yuren/gpt-5.6-luna": luna_profile,
    }
    app_config = type(
        "AppConfig",
        (),
        {
            "model_catalog": lambda self: type(
                "Catalog", (), {"require": lambda self, ref: profiles[ref]}
            )()
        },
    )()
    terra_provider = object()
    luna_provider = object()
    monkeypatch.setattr(runner, "load_config", lambda *, project_root: app_config)
    monkeypatch.setattr(
        runner,
        "create_provider_for_model",
        lambda received_config, received_profile: (
            terra_provider if received_profile is terra_profile else luna_provider
        ),
    )
    config = _run_config(
        tmp_path,
        model="Yuren/gpt-5.6-terra",
        classifier_model="Yuren/gpt-5.6-luna",
        request_options=MainRequestOptions(max_tokens=4_096),
    )

    resolved = _resolve_providers(config)

    assert resolved.main_provider is terra_provider
    assert resolved.classifier_provider is luna_provider
    assert resolved.classifier_model == "Yuren/gpt-5.6-luna"
    assert resolved.main_options.temperature == 0.35
    assert resolved.main_options.max_tokens == 4_096
    assert resolved.main_options.extra_body == {"reasoning_effort": "medium"}
    assert resolved.classifier_options.temperature == 0.0
    assert resolved.classifier_options.extra_body == {"reasoning_effort": "low"}
    assert terra_request.max_tokens == 8_192
    assert luna_request.max_tokens == 1_024


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


def test_usage_incomplete_outranks_verifier_failure_for_token_aggregation() -> None:
    assert _trial_status(
        arm=Arm.FULL,
        expected_boundary=True,
        boundary_change_count=1,
        confounded_auto=False,
        provider_error=False,
        verifier_exit_code=1,
        usage_complete=False,
    ) == "usage_incomplete"
