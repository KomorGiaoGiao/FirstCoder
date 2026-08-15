"""离线汇总 Harbor 运行结果与 FirstCoder 回合遥测。"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable


@dataclass(slots=True)
class HarborRunSummary:
    run_dir: str
    total_trials: int
    environment_successes: int
    agent_setup_successes: int
    explicit_reward_count: int
    reward_passes: int
    exception_categories: dict[str, int] = field(default_factory=dict)
    failure_categories: dict[str, int] = field(default_factory=dict)
    telemetry: dict[str, Any] | None = None

    @property
    def environment_success_rate(self) -> float:
        return _ratio(self.environment_successes, self.total_trials)

    @property
    def agent_setup_success_rate(self) -> float:
        return _ratio(self.agent_setup_successes, self.total_trials)

    @property
    def agent_setup_conditional_rate(self) -> float:
        return _ratio(self.agent_setup_successes, self.environment_successes)

    @property
    def reward_only_pass_rate(self) -> float:
        return _ratio(self.reward_passes, self.explicit_reward_count)

    @property
    def end_to_end_pass_rate(self) -> float:
        return _ratio(self.reward_passes, self.total_trials)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(
            environment_success_rate=self.environment_success_rate,
            agent_setup_success_rate=self.agent_setup_success_rate,
            agent_setup_conditional_rate=self.agent_setup_conditional_rate,
            reward_only_pass_rate=self.reward_only_pass_rate,
            end_to_end_pass_rate=self.end_to_end_pass_rate,
        )
        return payload


def summarize_run(run_dir: str | Path) -> HarborRunSummary:
    root = Path(run_dir).expanduser().resolve()
    trial_dirs = sorted(path.parent for path in root.glob("*/result.json"))
    environment_successes = 0
    agent_setup_successes = 0
    explicit_reward_count = 0
    reward_passes = 0
    exceptions: Counter[str] = Counter()
    failures: Counter[str] = Counter()

    for trial_dir in trial_dirs:
        result = _read_json(trial_dir / "result.json")
        config = _read_json(trial_dir / "config.json", required=False)
        environment_ok = _environment_succeeded(result)
        setup_ok = _agent_setup_succeeded(result, install_only=bool(config.get("install_only")))
        environment_successes += int(environment_ok)
        agent_setup_successes += int(setup_ok)
        exception = result.get("exception_info")
        if isinstance(exception, dict):
            exceptions[str(exception.get("exception_type") or "unknown")] += 1
        reward = _explicit_reward(result)
        if reward is not None:
            explicit_reward_count += 1
            reward_passes += int(reward > 0)
        failures[_failure_category(result, reward, environment_ok, setup_ok)] += 1

    return HarborRunSummary(
        run_dir=str(root),
        total_trials=len(trial_dirs),
        environment_successes=environment_successes,
        agent_setup_successes=agent_setup_successes,
        explicit_reward_count=explicit_reward_count,
        reward_passes=reward_passes,
        exception_categories=dict(exceptions.most_common()),
        failure_categories=dict(failures.most_common()),
        telemetry=_summarize_telemetry(trial_dirs),
    )


def compare_runs(baseline: HarborRunSummary, candidate: HarborRunSummary) -> dict[str, float]:
    return {
        "environment_success_rate_delta": candidate.environment_success_rate - baseline.environment_success_rate,
        "agent_setup_success_rate_delta": candidate.agent_setup_success_rate - baseline.agent_setup_success_rate,
        "reward_only_pass_rate_delta": candidate.reward_only_pass_rate - baseline.reward_only_pass_rate,
        "end_to_end_pass_rate_delta": candidate.end_to_end_pass_rate - baseline.end_to_end_pass_rate,
    }


def render_markdown(summary: HarborRunSummary) -> str:
    lines = [
        "# Harbor 运行汇总",
        "",
        f"- 运行目录：`{summary.run_dir}`",
        f"- trial 总数：{summary.total_trials}",
        f"- 环境成功率：{summary.environment_successes}/{summary.total_trials} ({_percent(summary.environment_success_rate)})",
        (
            f"- agent setup 成功率：{summary.agent_setup_successes}/{summary.total_trials} "
            f"({_percent(summary.agent_setup_success_rate)})；环境成功后的条件成功率 "
            f"{_percent(summary.agent_setup_conditional_rate)}"
        ),
        f"- reward-only：{summary.reward_passes}/{summary.explicit_reward_count} ({_percent(summary.reward_only_pass_rate)})",
        f"- 端到端：{summary.reward_passes}/{summary.total_trials} ({_percent(summary.end_to_end_pass_rate)})",
        "",
        "## 失败分类",
        "",
        *_counter_lines(summary.failure_categories),
        "",
        "## 异常类型",
        "",
        *_counter_lines(summary.exception_categories),
    ]
    if summary.telemetry is not None:
        telemetry = summary.telemetry
        lines.extend(
            [
                "",
                "## FirstCoder 遥测",
                "",
                f"- 含遥测 trial：{telemetry['trials_with_telemetry']}",
                f"- 最终回合快照：{telemetry['turns']}",
                f"- 平均 provider 调用/重试：{telemetry['average_provider_calls']:.2f} / {telemetry['average_provider_retries']:.2f}",
                f"- 平均工具调用/失败：{telemetry['average_tool_calls']:.2f} / {telemetry['average_tool_failures']:.2f}",
                f"- 平均重复工具调用：{telemetry['average_repeated_tool_calls']:.2f}",
                f"- 使用完成门禁的回合：{telemetry['completion_gate_turns']}",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_comparison(baseline: HarborRunSummary, candidate: HarborRunSummary) -> str:
    deltas = compare_runs(baseline, candidate)
    return "\n".join(
        [
            "# Harbor A/B 对比",
            "",
            f"- 基线：`{baseline.run_dir}`",
            f"- 候选：`{candidate.run_dir}`",
            f"- 环境成功率变化：{_signed_percent(deltas['environment_success_rate_delta'])}",
            f"- agent setup 成功率变化：{_signed_percent(deltas['agent_setup_success_rate_delta'])}",
            f"- reward-only 变化：{_signed_percent(deltas['reward_only_pass_rate_delta'])}",
            f"- 端到端变化：{_signed_percent(deltas['end_to_end_pass_rate_delta'])}",
            "",
        ]
    )


def _environment_succeeded(result: dict[str, Any]) -> bool:
    return _phase_finished(result.get("environment_setup")) and result.get("agent_setup") is not None


def _agent_setup_succeeded(result: dict[str, Any], *, install_only: bool) -> bool:
    if not _phase_finished(result.get("agent_setup")):
        return False
    if install_only:
        return result.get("exception_info") is None
    return result.get("agent_execution") is not None


def _phase_finished(value: object) -> bool:
    return isinstance(value, dict) and bool(value.get("finished_at"))


def _explicit_reward(result: dict[str, Any]) -> float | None:
    verifier_result = result.get("verifier_result")
    rewards = verifier_result.get("rewards") if isinstance(verifier_result, dict) else None
    if not isinstance(rewards, dict):
        return None
    if _is_number(rewards.get("reward")):
        return float(rewards["reward"])
    numeric = [float(value) for value in rewards.values() if _is_number(value)]
    return fmean(numeric) if numeric else None


def _failure_category(
    result: dict[str, Any],
    reward: float | None,
    environment_ok: bool,
    setup_ok: bool,
) -> str:
    if reward is not None:
        return "passed" if reward > 0 else "reward_zero"
    if not environment_ok:
        return "environment_setup"
    if not setup_ok:
        return "agent_setup"
    if not _phase_finished(result.get("agent_execution")):
        return "agent_execution"
    if not _phase_finished(result.get("verifier")):
        return "verifier"
    return "reward_missing"


def _summarize_telemetry(trial_dirs: Iterable[Path]) -> dict[str, Any] | None:
    latest: dict[tuple[str, str, int], tuple[int, dict[str, Any]]] = {}
    trials_with_telemetry = 0
    for trial_dir in trial_dirs:
        path = trial_dir / "agent" / "firstcoder-session.jsonl"
        if not path.is_file():
            continue
        found = False
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                payload = event.get("payload")
                if event.get("type") != "agent_turn_telemetry" or not isinstance(payload, dict):
                    continue
                found = True
                key = (
                    trial_dir.name,
                    str(event.get("session_id") or trial_dir.name),
                    _safe_int(payload.get("turn_number")),
                )
                if key not in latest or line_number >= latest[key][0]:
                    latest[key] = (line_number, payload)
        trials_with_telemetry += int(found)
    if not latest:
        return None
    payloads = [item[1] for item in latest.values()]
    return {
        "trials_with_telemetry": trials_with_telemetry,
        "turns": len(payloads),
        "status_counts": dict(Counter(str(item.get("status") or "unknown") for item in payloads).most_common()),
        "stop_reason_counts": dict(Counter(str(item.get("stop_reason") or "unknown") for item in payloads).most_common()),
        "provider_failure_categories": dict(Counter(str(item["provider_failure_category"]) for item in payloads if item.get("provider_failure_category")).most_common()),
        "average_provider_calls": _average(payloads, "provider_calls"),
        "average_provider_retries": _average(payloads, "provider_retries"),
        "average_tool_calls": _average(payloads, "tool_calls"),
        "average_tool_failures": _average(payloads, "tool_failures"),
        "average_repeated_tool_calls": _average(payloads, "repeated_tool_calls"),
        "average_validation_count": _average(payloads, "validation_count"),
        "average_elapsed_seconds": _average(payloads, "elapsed_seconds"),
        "completion_gate_turns": sum(bool(item.get("completion_gate_used")) for item in payloads),
    }


def _average(payloads: list[dict[str, Any]], key: str) -> float:
    return fmean(float(item.get(key) or 0) for item in payloads)


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _read_json(path: Path, *, required: bool = True) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise FileNotFoundError(path)
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点必须是对象：{path}")
    return value


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _percent(value: float) -> str:
    return f"{value * 100:.2f}%"


def _signed_percent(value: float) -> str:
    return f"{value * 100:+.2f} 个百分点"


def _counter_lines(values: dict[str, int]) -> list[str]:
    return [f"- `{name}`：{count}" for name, count in values.items()] or ["- 无"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汇总 Harbor result.json 与 FirstCoder 遥测")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    baseline = summarize_run(args.run_dir)
    if args.compare is None:
        payload: object = baseline.to_dict()
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n" if args.json else render_markdown(baseline)
    else:
        candidate = summarize_run(args.compare)
        payload = {"baseline": baseline.to_dict(), "candidate": candidate.to_dict(), "deltas": compare_runs(baseline, candidate)}
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n" if args.json else render_comparison(baseline, candidate)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
