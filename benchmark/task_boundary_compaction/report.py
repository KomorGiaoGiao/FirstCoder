"""Aggregate raw task-boundary trial results without hiding invalid trials."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median
from typing import Iterable

from benchmark.task_boundary_compaction.models import Arm, TrialResult
from benchmark.task_boundary_compaction.provider_observer import usage_totals


_EXCLUDED_STATUSES = frozenset(
    {"confounded_auto", "invalid_boundary", "provider_error", "verifier_error", "usage_incomplete"}
)


def build_report(results: Iterable[TrialResult], *, output_dir: str | Path) -> dict[str, object]:
    """Write JSON/Markdown summaries while retaining all raw trial statuses."""

    result_list = list(results)
    summary = {
        "trial_count": len(result_list),
        "arms": {arm.value: _arm_summary(result_list, arm) for arm in Arm},
        "deltas": _deltas(result_list),
        "raw_status_counts": _status_counts(result_list),
    }
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (destination / "summary.md").write_text(_render_markdown(summary), encoding="utf-8")
    return summary


def load_results(input_dir: str | Path) -> list[TrialResult]:
    """Read recursively written raw result files, excluding derived summaries."""

    return [
        TrialResult.from_dict(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(Path(input_dir).rglob("result.json"))
    ]


def _arm_summary(results: list[TrialResult], arm: Arm) -> dict[str, object]:
    arm_results = [result for result in results if result.arm is arm]
    eligible = [result for result in arm_results if result.status not in _EXCLUDED_STATUSES]
    all_tokens = [_trial_total_tokens(result) for result in eligible]
    classifier_tokens = [_trial_kind_total_tokens(result, "classifier") for result in eligible]
    l4_tokens = [_trial_kind_total_tokens(result, "l4") for result in eligible]
    elapsed = [result.elapsed_seconds for result in eligible]
    return {
        "trial_count": len(arm_results),
        "eligible_trial_count": len(eligible),
        "pass_count": sum(result.status == "passed" for result in arm_results),
        "excluded_status_counts": _status_counts(arm_results, excluded_only=True),
        "all_provider_total_tokens_median": _median_or_none(all_tokens),
        "classifier_total_tokens_median": _median_or_none(classifier_tokens),
        "l4_total_tokens_median": _median_or_none(l4_tokens),
        "elapsed_seconds_p50": _percentile_or_none(elapsed, 0.50),
        "elapsed_seconds_p95": _percentile_or_none(elapsed, 0.95),
        "task_hash_changed_compaction_count": sum(
            metric.trigger == "task_hash_changed"
            for result in arm_results
            for metric in result.compactions
        ),
        "task_boundary_observed_count": sum(result.boundary_event_count for result in arm_results),
        "task_hash_changed_observed_count": sum(result.task_hash_changed_count for result in arm_results),
        "agent_turn_telemetry_count": sum(result.agent_turn_telemetry_count for result in arm_results),
    }


def _deltas(results: list[TrialResult]) -> dict[str, dict[str, float | None]]:
    return {
        "full_minus_classifier_only": _delta(results, Arm.FULL, Arm.CLASSIFIER_ONLY),
        "classifier_only_minus_auto_only": _delta(results, Arm.CLASSIFIER_ONLY, Arm.AUTO_ONLY),
        "full_minus_auto_only": _delta(results, Arm.FULL, Arm.AUTO_ONLY),
    }


def _delta(results: list[TrialResult], left: Arm, right: Arm) -> dict[str, float | None]:
    left_values = [_trial_total_tokens(result) for result in results if result.arm is left and result.status not in _EXCLUDED_STATUSES]
    right_values = [_trial_total_tokens(result) for result in results if result.arm is right and result.status not in _EXCLUDED_STATUSES]
    left_median = _median_or_none(left_values)
    right_median = _median_or_none(right_values)
    return {
        "all_provider_total_tokens": (
            None if left_median is None or right_median is None else left_median - right_median
        )
    }


def _trial_total_tokens(result: TrialResult) -> int | None:
    return usage_totals(result.provider_calls)["all"].total_tokens


def _trial_kind_total_tokens(result: TrialResult, kind: str) -> int | None:
    return usage_totals(result.provider_calls)[kind].total_tokens


def _median_or_none(values: list[int | None]) -> float | None:
    known = [value for value in values if value is not None]
    return None if not known or len(known) != len(values) else float(median(known))


def _percentile_or_none(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * percentile))
    return ordered[index]


def _status_counts(results: Iterable[TrialResult], *, excluded_only: bool = False) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        if excluded_only and result.status not in _EXCLUDED_STATUSES:
            continue
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


def _render_markdown(summary: dict[str, object]) -> str:
    arms = summary["arms"]
    assert isinstance(arms, dict)
    rows = [
        "# 任务边界压缩基准汇总",
        "",
        "| Arm | Trial | Pass | Eligible | Provider token P50 | P95 耗时（秒） | TASK_HASH_CHANGED |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for arm in Arm:
        data = arms[arm.value]
        assert isinstance(data, dict)
        rows.append(
            "| {arm} | {trials} | {passed} | {eligible} | {tokens} | {p95} | {trigger} |".format(
                arm=arm.value,
                trials=data["trial_count"],
                passed=data["pass_count"],
                eligible=data["eligible_trial_count"],
                tokens=data["all_provider_total_tokens_median"],
                p95=data["elapsed_seconds_p95"],
                trigger=data["task_hash_changed_compaction_count"],
            )
        )
    rows.extend(["", "## 因果差值", ""])
    deltas = summary["deltas"]
    assert isinstance(deltas, dict)
    for name, data in deltas.items():
        assert isinstance(data, dict)
        rows.append(f"- `{name}`：全 provider token 中位数差值 = {data['all_provider_total_tokens']}")
    return "\n".join(rows) + "\n"


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build task-boundary compaction benchmark reports.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    arguments = _parse_arguments()
    summary = build_report(load_results(arguments.input), output_dir=arguments.output)
    print(json.dumps({"trials": summary["trial_count"], "output": str(arguments.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
