from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from benchmark.task_boundary_compaction.cases import ControlledCase, RepositoryFile
from benchmark.task_boundary_compaction.models import Arm, BenchmarkCase, TurnSpec
from benchmark.task_boundary_compaction.report import build_report
from benchmark.task_boundary_compaction.runner import RunConfig, run_case
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse, TokenUsage


@dataclass
class MatrixProvider(ChatProvider):
    decisions: list[str] = field(default_factory=lambda: ["new", "same"])
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
            return ChatResponse(
                provider=self.name,
                model=self.model,
                content=(
                    '{"decision":"'
                    + self.decisions.pop(0)
                    + '","basis_message_id":"'
                    + _basis_message_id(request)
                    + '"}'
                ),
                usage=TokenUsage(input_tokens=5, output_tokens=1, total_tokens=6),
            )
        return ChatResponse(
            provider=self.name,
            model=self.model,
            content="已检查实现。",
            usage=TokenUsage(input_tokens=10, output_tokens=2, total_tokens=12),
        )


def _basis_message_id(request: ChatRequest) -> str:
    for message in reversed(request.messages):
        match = re.search(r"basis_message_id=([A-Za-z0-9_]+)", message.content)
        if match:
            return match.group(1)
    raise AssertionError("classifier request did not expose basis_message_id")


def _passing_controlled_case() -> ControlledCase:
    return ControlledCase(
        benchmark_case=BenchmarkCase(
            case_id="fake-controlled",
            kind="controlled",
            turns=(
                TurnSpec(message="任务 B：检查已有实现。", expected_decision="new"),
                TurnSpec(message="继续任务 B：运行测试。", expected_decision="same"),
            ),
            verify_command=("python", "-m", "pytest", "-q"),
            expected_boundary=True,
        ),
        repository_files=(
            RepositoryFile(path="subject.py", content="VALUE = 1\n"),
            RepositoryFile(
                path="tests/test_subject.py",
                content="from subject import VALUE\n\n\ndef test_value_is_ready():\n    assert VALUE == 1\n",
            ),
        ),
    )


def test_run_case_writes_isolated_results_and_builds_causal_summary(tmp_path) -> None:
    case = _passing_controlled_case()
    results = []
    for arm in Arm:
        config = RunConfig(
            output_root=tmp_path / "runs",
            run_id="fake-matrix",
            project_root=Path.cwd(),
            model="fake/fake-model",
            context_window=32_768,
            provider_factory=MatrixProvider,
        )
        results.append(run_case(case, arm=arm, config=config))

    by_arm = {result.arm: result for result in results}
    full = by_arm[Arm.FULL]
    classifier_only = by_arm[Arm.CLASSIFIER_ONLY]

    assert all(result.status == "passed" for result in results)
    assert all(
        (tmp_path / "runs" / "fake-matrix" / "fake-controlled" / result.arm.value / "result.json").is_file()
        for result in results
    )
    assert full.boundary_event_count == 2
    assert full.task_hash_changed_count == 1
    assert full.agent_turn_telemetry_count == 2
    assert any(metric.trigger == "task_hash_changed" for metric in full.compactions)
    assert not any(metric.trigger == "task_hash_changed" for metric in classifier_only.compactions)
    assert all("data" in result.artifact_paths["data_root"] for result in results)

    summary = build_report(results, output_dir=tmp_path / "summary")

    assert (tmp_path / "summary" / "summary.json").is_file()
    assert (tmp_path / "summary" / "summary.md").is_file()
    assert summary["deltas"]["full_minus_classifier_only"]["all_provider_total_tokens"] <= 0
    assert summary["arms"]["full"]["pass_count"] == 1


def test_report_keeps_a_valid_verifier_failure_in_token_aggregates(tmp_path) -> None:
    from benchmark.task_boundary_compaction.models import ProviderCallMetric, TrialResult

    result = TrialResult(
        case_id="quality-failure",
        arm=Arm.FULL,
        model="fake/fake-model",
        context_window=32_768,
        status="verifier_failed",
        verifier_exit_code=1,
        verifier_stdout_sha256="a" * 64,
        verifier_stderr_sha256="b" * 64,
        provider_calls=(
            ProviderCallMetric(
                kind="main",
                input_tokens=10,
                output_tokens=2,
                total_tokens=12,
                elapsed_seconds=0.1,
            ),
        ),
        usage_complete=True,
        artifact_paths={"result": str(tmp_path / "result.json")},
    )

    summary = build_report([result], output_dir=tmp_path / "summary")

    assert summary["arms"]["full"]["pass_count"] == 0
    assert summary["arms"]["full"]["eligible_trial_count"] == 1
    assert summary["arms"]["full"]["all_provider_total_tokens_median"] == 12.0
