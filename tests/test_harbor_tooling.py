from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from benchmark.harbor.shared.preflight import build_mounts, render_report, run_preflight
from benchmark.harbor.shared.prepare_wheelhouse import build_download_command, project_requirements
from benchmark.harbor.shared.summarize import compare_runs, summarize_run
from benchmark.harbor.terminal_bench.run_terminal_bench_ab import FIXED_TASKS, build_harbor_command


class _Response:
    def __init__(self, payload: object | None = None) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def close(self) -> None:
        pass


def _successful_runner(command, **_kwargs):
    if command[:2] == ["docker", "version"]:
        return subprocess.CompletedProcess(command, 0, stdout="27.5.1\n", stderr="")
    if command[:3] == ["docker", "image", "inspect"]:
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")
    raise AssertionError(command)


def _successful_opener(request, **_kwargs):
    if request.full_url.endswith("/models"):
        return _Response({"data": [{"id": "test-model"}]})
    return _Response()


def _write_env(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "FIRSTCODER_PROVIDER=openai-compatible",
                "FIRSTCODER_PROVIDER_NAME=test-provider",
                "FIRSTCODER_MODEL=test-model",
                "FIRSTCODER_BASE_URL=https://example.test/v1",
                "FIRSTCODER_API_KEY=super-secret-value",
            ]
        ),
        encoding="utf-8",
    )


def test_preflight_checks_provider_docker_cache_network_and_images_without_leaking_secret(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.harbor"
    _write_env(env_file)

    report = run_preflight(
        env_file=env_file,
        cache_dir=tmp_path / "cache",
        images=["example/image:1"],
        command_runner=_successful_runner,
        url_opener=_successful_opener,
    )

    assert report.ok
    assert [check.status for check in report.checks] == [
        "pass",
        "pass",
        "pass",
        "pass",
        "warn",
        "pass",
        "pass",
        "pass",
    ]
    rendered = render_report(report)
    assert "super-secret-value" not in rendered
    assert "FIRSTCODER_API_KEY" not in rendered
    assert (tmp_path / "cache").is_dir()


def test_preflight_fails_when_configured_model_is_absent_from_provider_catalog(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.harbor"
    _write_env(env_file)

    def opener(request, **_kwargs):
        if request.full_url.endswith("/models"):
            return _Response({"data": [{"id": "different-model"}]})
        return _Response()

    report = run_preflight(
        env_file=env_file,
        cache_dir=tmp_path / "cache",
        command_runner=_successful_runner,
        url_opener=opener,
    )

    assert not report.ok
    checks = {check.name: check for check in report.checks}
    assert checks["provider_model"].status == "fail"
    assert "test-model" in checks["provider_model"].message
    assert "super-secret-value" not in render_report(report)


def test_preflight_fails_for_placeholder_provider_values_and_required_empty_wheelhouse(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.harbor"
    env_file.write_text(
        "FIRSTCODER_PROVIDER=openai-compatible\n"
        "FIRSTCODER_PROVIDER_NAME=your-provider\n"
        "FIRSTCODER_MODEL=your-model\n"
        "FIRSTCODER_BASE_URL=https://provider.example/v1\n"
        "FIRSTCODER_API_KEY=replace-with-your-api-key\n",
        encoding="utf-8",
    )
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()

    report = run_preflight(
        env_file=env_file,
        cache_dir=tmp_path / "cache",
        wheelhouse_dir=wheelhouse,
        wheelhouse_only=True,
        probe_network=False,
        command_runner=_successful_runner,
    )

    assert not report.ok
    checks = {check.name: check for check in report.checks}
    assert checks["provider_variables"].status == "fail"
    assert checks["wheelhouse"].status == "fail"


def test_build_mounts_uses_writable_cache_and_read_only_wheelhouse(tmp_path: Path) -> None:
    mounts = build_mounts(tmp_path / "cache", tmp_path / "wheelhouse")

    assert mounts[0]["target"] == "/opt/firstcoder-cache"
    assert "read_only" not in mounts[0]
    assert mounts[1]["target"] == "/opt/firstcoder-wheelhouse"
    assert mounts[1]["read_only"] is True


def test_summarize_run_separates_infrastructure_reward_only_and_end_to_end_rates(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_trial(run_dir / "pass", reward=1.0, telemetry=True)
    _write_trial(run_dir / "fail", reward=0.0)
    _write_trial(run_dir / "infra", environment_ok=False, exception_type="RuntimeError")

    summary = summarize_run(run_dir)

    assert summary.total_trials == 3
    assert summary.environment_successes == 2
    assert summary.agent_setup_successes == 2
    assert summary.explicit_reward_count == 2
    assert summary.reward_passes == 1
    assert summary.reward_only_pass_rate == pytest.approx(0.5)
    assert summary.end_to_end_pass_rate == pytest.approx(1 / 3)
    assert summary.failure_categories == {"passed": 1, "reward_zero": 1, "environment_setup": 1}
    assert summary.exception_categories == {"RuntimeError": 1}
    assert summary.telemetry is not None
    assert summary.telemetry["turns"] == 1
    assert summary.telemetry["average_provider_calls"] == 3
    assert summary.telemetry["average_tool_calls"] == 4
    assert summary.telemetry["status_counts"] == {"completed": 1}


def test_compare_runs_reports_rate_deltas(tmp_path: Path) -> None:
    baseline_dir = tmp_path / "baseline"
    candidate_dir = tmp_path / "candidate"
    _write_trial(baseline_dir / "zero", reward=0.0)
    _write_trial(candidate_dir / "one", reward=1.0)

    deltas = compare_runs(summarize_run(baseline_dir), summarize_run(candidate_dir))

    assert deltas["reward_only_pass_rate_delta"] == 1.0
    assert deltas["end_to_end_pass_rate_delta"] == 1.0


def test_prepare_wheelhouse_reads_runtime_and_build_requirements(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[build-system]\nrequires = ['setuptools>=68', 'wheel']\n"
        "[project]\nname = 'demo'\ndependencies = ['anyio', 'PyYAML']\n",
        encoding="utf-8",
    )

    requirements = project_requirements(pyproject)
    command = build_download_command(pyproject=pyproject, output=tmp_path / "wheelhouse")

    assert requirements == ["setuptools>=68", "wheel", "anyio", "PyYAML"]
    assert "--platform" in command
    assert "manylinux2014_x86_64" in command
    assert "--python-version" in command
    assert "311" in command
    assert command[-4:] == requirements


def test_fixed_ab_command_is_single_concurrency_and_uses_safe_env_references(tmp_path: Path) -> None:
    command = build_harbor_command(
        harbor_executable="harbor",
        env_file=tmp_path / ".env.harbor",
        output_dir=tmp_path / "runs",
        cache_dir=tmp_path / "cache",
        wheelhouse_dir=tmp_path / "wheelhouse",
        provider_name="provider",
        model="model",
        max_tool_rounds=120,
        reasoning_effort="medium",
        wheelhouse_only=True,
    )

    assert command[:4] == ["harbor", "run", "--dataset", "terminal-bench@2.0"]
    included = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "--include-task-name"]
    assert tuple(included) == FIXED_TASKS
    assert command[command.index("--n-concurrent") + 1] == "1"
    assert command[command.index("--agent-timeout-multiplier") + 1] == "4"
    agent_kwargs = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--agent-kwarg"
    ]
    assert "max_turn_seconds=3300" in agent_kwargs
    assert "FIRSTCODER_API_KEY=${FIRSTCODER_API_KEY}" in command
    assert "FIRSTCODER_MODEL=model" in command
    assert "FIRSTCODER_DISABLE_GLOBAL_SKILLS=${FIRSTCODER_DISABLE_GLOBAL_SKILLS:-1}" in command
    assert "FIRSTCODER_WHEELHOUSE_ONLY=1" in command
    mounts = json.loads(command[command.index("--mounts") + 1])
    assert mounts[1]["read_only"] is True


def test_fixed_ab_command_omits_provider_specific_reasoning_default(tmp_path: Path) -> None:
    command = build_harbor_command(
        harbor_executable="harbor",
        env_file=tmp_path / ".env.harbor",
        output_dir=tmp_path / "runs",
        cache_dir=tmp_path / "cache",
        wheelhouse_dir=None,
        provider_name="provider",
        model="model",
        max_tool_rounds=120,
        reasoning_effort=None,
        wheelhouse_only=False,
    )

    assert not any(value.startswith("reasoning_effort=") for value in command)


def test_fixed_ab_command_can_select_a_regression_subset(tmp_path: Path) -> None:
    command = build_harbor_command(
        harbor_executable="harbor",
        env_file=tmp_path / ".env.harbor",
        output_dir=tmp_path / "runs",
        cache_dir=tmp_path / "cache",
        wheelhouse_dir=None,
        provider_name="provider",
        model="model",
        max_tool_rounds=120,
        reasoning_effort="high",
        wheelhouse_only=False,
        tasks=("configure-git-webserver", "compile-compcert"),
    )

    included = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--include-task-name"
    ]
    assert included == ["configure-git-webserver", "compile-compcert"]


def _write_trial(
    trial_dir: Path,
    *,
    reward: float | None = None,
    environment_ok: bool = True,
    exception_type: str | None = None,
    telemetry: bool = False,
) -> None:
    trial_dir.mkdir(parents=True)
    phase = {"started_at": "2026-08-10T00:00:00Z", "finished_at": "2026-08-10T00:00:01Z"}
    result = {
        "environment_setup": phase,
        "agent_setup": phase if environment_ok else None,
        "agent_execution": phase if environment_ok else None,
        "verifier": phase if environment_ok else None,
        "verifier_result": {"rewards": {"reward": reward}} if reward is not None else None,
        "exception_info": {"exception_type": exception_type} if exception_type else None,
    }
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    (trial_dir / "config.json").write_text(json.dumps({"install_only": False}), encoding="utf-8")
    if telemetry:
        agent_dir = trial_dir / "agent"
        agent_dir.mkdir()
        events = [
            {
                "session_id": "sess",
                "type": "agent_turn_telemetry",
                "payload": {
                    "turn_number": 1,
                    "snapshot_index": 1,
                    "status": "paused",
                    "provider_calls": 2,
                    "tool_calls": 2,
                },
            },
            {
                "session_id": "sess",
                "type": "agent_turn_telemetry",
                "payload": {
                    "turn_number": 1,
                    "snapshot_index": 2,
                    "status": "completed",
                    "stop_reason": "stop",
                    "provider_calls": 3,
                    "provider_retries": 1,
                    "tool_calls": 4,
                    "tool_failures": 1,
                    "repeated_tool_calls": 1,
                    "validation_count": 1,
                    "elapsed_seconds": 5,
                    "completion_gate_used": True,
                },
            },
        ]
        (agent_dir / "firstcoder-session.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
