from __future__ import annotations

import pytest

from benchmark.task_boundary_compaction.models import (
    Arm,
    BenchmarkCase,
    CompactionMetric,
    ProviderCallMetric,
    TrialResult,
    TurnSpec,
)


def test_positive_case_requires_new_then_same_turns() -> None:
    case = BenchmarkCase(
        case_id="controlled-parser",
        kind="controlled",
        turns=(
            TurnSpec(message="任务 B：分析 parser bug", expected_decision="new"),
            TurnSpec(message="继续任务 B：修复并验证", expected_decision="same"),
        ),
        verify_command=(".venv/bin/python", "-m", "pytest", "tests/test_parser.py", "-q"),
        expected_boundary=True,
    )

    assert case.turns[-1].expected_decision == "same"
    assert Arm.FULL.value == "full"


@pytest.mark.parametrize(
    ("kwargs", "field_name"),
    [
        ({"case_id": ""}, "case_id"),
        ({"turns": ()}, "turns"),
        ({"verify_command": ()}, "verify_command"),
        (
            {
                "turns": (
                    TurnSpec(message="任务 B", expected_decision="new"),
                    TurnSpec(message="继续任务 B", expected_decision="new"),
                )
            },
            "turns",
        ),
        (
            {
                "turns": (
                    TurnSpec(message="继续任务 A", expected_decision="new"),
                    TurnSpec(message="继续任务 A 并验证", expected_decision="same"),
                ),
                "expected_boundary": False,
            },
            "turns",
        ),
    ],
)
def test_case_rejects_invalid_benchmark_contract(
    kwargs: dict[str, object],
    field_name: str,
) -> None:
    defaults: dict[str, object] = {
        "case_id": "controlled-parser",
        "kind": "controlled",
        "turns": (
            TurnSpec(message="任务 B：分析 parser bug", expected_decision="new"),
            TurnSpec(message="继续任务 B：修复并验证", expected_decision="same"),
        ),
        "verify_command": (".venv/bin/python", "-m", "pytest", "-q"),
        "expected_boundary": True,
    }

    with pytest.raises(ValueError, match=field_name):
        BenchmarkCase(**(defaults | kwargs))


def test_trial_result_round_trips_without_provider_or_secret_data() -> None:
    result = TrialResult(
        case_id="controlled-parser",
        arm=Arm.FULL,
        model="Yuren/gpt-5.6-terra",
        context_window=32_768,
        status="passed",
        verifier_exit_code=0,
        verifier_stdout_sha256="a" * 64,
        verifier_stderr_sha256="b" * 64,
        provider_calls=(
            ProviderCallMetric(
                kind="main",
                input_tokens=120,
                output_tokens=24,
                total_tokens=144,
                elapsed_seconds=1.25,
            ),
        ),
        compactions=(
            CompactionMetric(
                trigger="task_hash_changed",
                event_type="compaction_completed",
                completed=True,
            ),
        ),
        boundary_event_count=1,
        usage_complete=True,
        elapsed_seconds=2.5,
        artifact_paths={"data_root": "/tmp/data", "result": "/tmp/result.json"},
    )

    encoded = result.to_dict()

    assert TrialResult.from_dict(encoded) == result
    assert "api_key" not in encoded
    assert "provider" not in encoded
    assert encoded["provider_calls"][0]["kind"] == "main"


def test_trial_result_rejects_an_unknown_serialized_arm() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        TrialResult.from_dict({"case_id": "controlled-parser", "arm": "unsupported"})
