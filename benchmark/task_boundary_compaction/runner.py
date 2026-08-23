"""Execute isolated three-arm task-boundary compaction benchmark trials."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time
from typing import TypeAlias

from benchmark.task_boundary_compaction.cases import (
    ControlledCase,
    HistoricalCase,
    load_controlled_cases,
    load_historical_cases,
    materialize_controlled_case,
    materialize_historical_case,
)
from benchmark.task_boundary_compaction.loop import BenchmarkAgentLoop
from benchmark.task_boundary_compaction.models import Arm, CompactionMetric, TrialResult
from benchmark.task_boundary_compaction.provider_observer import RecordingProvider
from benchmark.task_boundary_compaction.seed import seed_old_task_context
from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.agent.session import AgentSession, create_project_permission_manager
from firstcoder.config.settings import load_config
from firstcoder.context.llm_compact import LlmCompactService
from firstcoder.context.manager import ContextWindowManager
from firstcoder.context.provider_summarizer import ProviderLlmCompactSummarizer
from firstcoder.context.store import JsonlSessionStore
from firstcoder.permissions.grants import FilePermissionGrantStore
from firstcoder.permissions.types import PermissionMode
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.factory import create_provider_for_model
from firstcoder.providers.types import MainRequestOptions
from firstcoder.tools.builtin import create_builtin_registry
from firstcoder.utils.sandbox_access import SandboxAccess


CaseDefinition: TypeAlias = ControlledCase | HistoricalCase
ProviderFactory: TypeAlias = Callable[[], ChatProvider]

_DEFAULT_MAX_TOOL_ROUNDS = 6
_DEFAULT_MAX_PROVIDER_CALLS = 12
_DEFAULT_MAX_TURN_SECONDS = 90.0
_DEFAULT_PROVIDER_TIMEOUT_SECONDS = 120.0


class BenchmarkValidityError(RuntimeError):
    """Raised after all trial artifacts exist but the required full-arm event is absent."""


@dataclass(frozen=True, slots=True)
class RunConfig:
    """All mutable state for a trial lives below ``output_root/run_id``."""

    output_root: Path
    run_id: str
    project_root: Path
    model: str
    context_window: int
    provider_factory: ProviderFactory | None = None
    request_options: MainRequestOptions = MainRequestOptions(max_tokens=4_096)
    random_seed: int = 0
    seed_fraction: float = 0.80
    repetition: int = 1
    max_tool_rounds: int = _DEFAULT_MAX_TOOL_ROUNDS
    max_provider_calls: int = _DEFAULT_MAX_PROVIDER_CALLS
    max_turn_seconds: float = _DEFAULT_MAX_TURN_SECONDS
    provider_timeout_seconds: float = _DEFAULT_PROVIDER_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must not be blank")
        if not self.model.strip():
            raise ValueError("model must not be blank")
        if self.context_window <= 0:
            raise ValueError("context_window must be positive")
        if not 0.75 <= self.seed_fraction < 0.90:
            raise ValueError("seed_fraction must be within the controlled safe range [0.75, 0.90)")
        if self.repetition <= 0:
            raise ValueError("repetition must be positive")
        if self.max_tool_rounds <= 0:
            raise ValueError("max_tool_rounds must be positive")
        if self.max_provider_calls <= 0:
            raise ValueError("max_provider_calls must be positive")
        if self.max_turn_seconds <= 0:
            raise ValueError("max_turn_seconds must be positive")
        if self.provider_timeout_seconds <= 0:
            raise ValueError("provider_timeout_seconds must be positive")


def run_case(case: CaseDefinition, *, arm: Arm, config: RunConfig) -> TrialResult:
    """Run one arm in a new private work area and retain only sanitized artifacts."""

    benchmark_case = case.benchmark_case
    trial_root = config.output_root / config.run_id / benchmark_case.case_id / arm.value
    if trial_root.exists():
        raise FileExistsError(f"benchmark trial directory already exists: {trial_root}")
    project_root = trial_root / "project"
    data_root = trial_root / "data"
    events_path = trial_root / "events.json"
    trial_root.mkdir(parents=True)
    try:
        _materialize_case(case, project_root=project_root, source_repo_root=config.project_root)

        provider, request_options = _resolve_provider(config)
        _configure_provider_timeout(provider, seconds=config.provider_timeout_seconds)
        recording_provider = RecordingProvider(provider)
        store = JsonlSessionStore(data_root)
        session = _create_benchmark_session(
            store=store,
            data_root=data_root,
            project_root=project_root,
        )
        context_manager = ContextWindowManager(
            store=store,
            l4_service=LlmCompactService(
                store=store,
                summarizer=ProviderLlmCompactSummarizer(recording_provider),
            ),
        )
        loop = BenchmarkAgentLoop(
            session=session,
            provider=recording_provider,
            tools=_benchmark_tools(project_root, data_root),
            context_manager=context_manager,
            request_options=request_options,
            context_window=config.context_window,
            limits=AgentLoopLimits(
                max_tool_rounds=config.max_tool_rounds,
                max_provider_calls=config.max_provider_calls,
                max_turn_seconds=config.max_turn_seconds,
            ),
            arm=arm,
        )
        initial_budget = loop.context_budget_for_view(session.rebuild_view())
        session.runtime_state.active_task_hash = f"seed_{benchmark_case.case_id}"
        seed_old_task_context(
            session,
            case_id=benchmark_case.case_id,
            target_input_tokens=int(initial_budget.high_watermark * config.seed_fraction),
            estimate_budget=loop.context_budget_for_view,
        )

        started_at = time.perf_counter()
        provider_error = False
        try:
            for turn in benchmark_case.turns:
                loop._run_user_turn_sync(turn.message)
        except BaseException:
            provider_error = True
        elapsed_seconds = time.perf_counter() - started_at
        verifier_exit_code, verifier_stdout, verifier_stderr = _run_verifier(
            benchmark_case.verify_command,
            cwd=project_root,
        )
        event_summary = _event_summary(store, session.session_id)
        _write_sanitized_events(store, session.session_id, events_path)
        usage_complete = all(
            metric.input_tokens is not None
            and metric.output_tokens is not None
            and metric.total_tokens is not None
            for metric in recording_provider.metrics
        )
        status = _trial_status(
            arm=arm,
            expected_boundary=benchmark_case.expected_boundary,
            boundary_change_count=event_summary.boundary_change_count,
            confounded_auto=event_summary.confounded_auto,
            provider_error=provider_error,
            verifier_exit_code=verifier_exit_code,
        )
        if status == "passed" and not usage_complete:
            status = "usage_incomplete"
        result = TrialResult(
            case_id=benchmark_case.case_id,
            arm=arm,
            model=config.model,
            context_window=config.context_window,
            status=status,
            verifier_exit_code=verifier_exit_code,
            verifier_stdout_sha256=_sha256(verifier_stdout),
            verifier_stderr_sha256=_sha256(verifier_stderr),
            provider_calls=tuple(recording_provider.metrics),
            compactions=event_summary.compactions,
            boundary_event_count=event_summary.boundary_event_count,
            task_hash_changed_count=event_summary.boundary_change_count,
            agent_turn_telemetry_count=event_summary.agent_turn_telemetry_count,
            usage_complete=usage_complete,
            elapsed_seconds=elapsed_seconds,
            repetition=config.repetition,
            max_tool_rounds=config.max_tool_rounds,
            max_provider_calls=config.max_provider_calls,
            max_turn_seconds=config.max_turn_seconds,
            provider_timeout_seconds=config.provider_timeout_seconds,
            artifact_paths={
                "trial_root": str(trial_root),
                "events": str(events_path),
                "result": str(trial_root / "result.json"),
            },
        )
        _write_result(result, trial_root / "result.json")
        return result
    finally:
        _remove_private_trial_directory(project_root, trial_root=trial_root)
        _remove_private_trial_directory(data_root, trial_root=trial_root)


def run_matrix(
    cases: Sequence[CaseDefinition],
    *,
    arms: Iterable[Arm] = tuple(Arm),
    repetitions: int,
    config: RunConfig,
) -> list[TrialResult]:
    """Run every case in a deterministic shuffled arm order without reusing a session."""

    if repetitions <= 0:
        raise ValueError("repetitions must be positive")
    results: list[TrialResult] = []
    randomizer = random.Random(config.random_seed)
    for repetition in range(1, repetitions + 1):
        arm_order = list(arms)
        randomizer.shuffle(arm_order)
        repetition_config = replace(
            config,
            run_id=config.run_id if repetitions == 1 else f"{config.run_id}-r{repetition}",
            repetition=repetition,
        )
        for case in cases:
            for arm in arm_order:
                results.append(run_case(case, arm=arm, config=repetition_config))
    _require_full_boundary_events(results, cases)
    return results


@dataclass(frozen=True, slots=True)
class _EventSummary:
    boundary_event_count: int
    boundary_change_count: int
    agent_turn_telemetry_count: int
    compactions: tuple[CompactionMetric, ...]
    confounded_auto: bool


def _materialize_case(case: CaseDefinition, *, project_root: Path, source_repo_root: Path) -> None:
    if isinstance(case, ControlledCase):
        materialize_controlled_case(case, destination=project_root)
        return
    materialize_historical_case(case, repo_root=source_repo_root, destination=project_root)


def _resolve_provider(config: RunConfig) -> tuple[ChatProvider, MainRequestOptions]:
    if config.provider_factory is not None:
        return config.provider_factory(), config.request_options
    app_config = load_config(project_root=config.project_root)
    profile = app_config.model_catalog().require(config.model)
    return (
        create_provider_for_model(app_config, profile),
        MainRequestOptions(
            temperature=profile.request.temperature,
            max_tokens=profile.request.max_tokens,
            extra_body=profile.request.extra_body,
        ),
    )


def _configure_provider_timeout(provider: ChatProvider, *, seconds: float) -> None:
    """Apply a request timeout only to SDK clients created for this benchmark trial."""

    client = getattr(provider, "_client", None)
    with_options = getattr(client, "with_options", None)
    if callable(with_options):
        setattr(provider, "_client", with_options(timeout=seconds))


def _create_benchmark_session(*, store: JsonlSessionStore, data_root: Path, project_root: Path) -> AgentSession:
    access = SandboxAccess()
    permission_manager = create_project_permission_manager(
        project_root,
        grants=FilePermissionGrantStore(data_root / "permissions.json"),
        mode=PermissionMode.BYPASS,
    )
    session = AgentSession.from_project(
        store=store,
        session_id="benchmark",
        project_root=project_root,
        tools=_benchmark_tools(project_root, data_root),
        permission_manager=permission_manager,
        sandbox_access=access,
    )
    session.require_prewrite_review = False
    return session


def _benchmark_tools(project_root: Path, data_root: Path):
    return create_builtin_registry(
        project_root,
        include_mutation_tools=True,
        include_execution_tools=True,
        include_network_tools=False,
        access=SandboxAccess(),
        include_ask_user=False,
        process_manager=None,
    ).tools()


def _run_verifier(command: tuple[str, ...], *, cwd: Path) -> tuple[int | None, str, str]:
    resolved_command = list(command)
    if resolved_command and resolved_command[0] == "python":
        resolved_command[0] = sys.executable
    environment = dict(os.environ)
    executable_directory = str(Path(sys.executable).parent)
    environment["PATH"] = executable_directory + os.pathsep + environment.get("PATH", "")
    try:
        completed = subprocess.run(
            resolved_command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
    except OSError as error:
        return None, "", str(error)
    return completed.returncode, completed.stdout, completed.stderr


def _event_summary(store: JsonlSessionStore, session_id: str) -> _EventSummary:
    boundary_event_count = 0
    boundary_change_count = 0
    agent_turn_telemetry_count = 0
    compactions: list[CompactionMetric] = []
    seen_boundary_change = False
    confounded_auto = False
    for event in store.list_events(session_id):
        if event.type == "task_boundary_observed":
            boundary_event_count += 1
            if bool(event.payload.get("should_trigger_compaction")):
                boundary_change_count += 1
                seen_boundary_change = True
        if event.type == "agent_turn_telemetry":
            agent_turn_telemetry_count += 1
        if event.type in {"compaction_completed", "llm_compaction_completed"}:
            trigger = str(event.payload.get("trigger") or "unknown")
            if trigger == "auto" and not seen_boundary_change:
                confounded_auto = True
            compactions.append(
                CompactionMetric(
                    trigger=trigger,
                    event_type=event.type,
                    completed=event.payload.get("status") == "success",
                )
            )
    return _EventSummary(
        boundary_event_count=boundary_event_count,
        boundary_change_count=boundary_change_count,
        agent_turn_telemetry_count=agent_turn_telemetry_count,
        compactions=tuple(compactions),
        confounded_auto=confounded_auto,
    )


def _write_sanitized_events(store: JsonlSessionStore, session_id: str, path: Path) -> None:
    """Persist only fields needed to audit boundary and compaction decisions."""

    records: list[dict[str, object]] = []
    for event in store.list_events(session_id):
        if event.type == "task_boundary_observed":
            records.append(
                {
                    "type": event.type,
                    "should_trigger_compaction": bool(event.payload.get("should_trigger_compaction")),
                }
            )
        elif event.type in {"compaction_completed", "llm_compaction_completed"}:
            records.append(
                {
                    "type": event.type,
                    "trigger": str(event.payload.get("trigger") or "unknown"),
                    "completed": event.payload.get("status") == "success",
                }
            )
        elif event.type == "agent_turn_telemetry":
            records.append({"type": event.type})
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _remove_private_trial_directory(path: Path, *, trial_root: Path) -> None:
    """Delete a known trial child without ever resolving a broad target."""

    resolved_trial_root = trial_root.resolve()
    resolved_path = path.resolve()
    if resolved_path.parent != resolved_trial_root or resolved_path.name not in {"project", "data"}:
        raise ValueError(f"refusing to remove non-trial private path: {path}")
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _trial_status(
    *,
    arm: Arm,
    expected_boundary: bool,
    boundary_change_count: int,
    confounded_auto: bool,
    provider_error: bool,
    verifier_exit_code: int | None,
) -> str:
    if provider_error:
        return "provider_error"
    if arm is not Arm.AUTO_ONLY:
        if expected_boundary and boundary_change_count != 1:
            return "invalid_boundary"
        if not expected_boundary and boundary_change_count != 0:
            return "invalid_boundary"
    if confounded_auto and expected_boundary and arm is not Arm.AUTO_ONLY:
        return "confounded_auto"
    if verifier_exit_code is None:
        return "verifier_error"
    if verifier_exit_code != 0:
        return "verifier_failed"
    return "passed"


def _require_full_boundary_events(
    results: Sequence[TrialResult],
    cases: Sequence[CaseDefinition],
) -> None:
    """Fail only after preserving every raw trial result needed for diagnosis."""

    expected_boundaries = {
        case.benchmark_case.case_id: case.benchmark_case.expected_boundary
        for case in cases
    }
    invalid_trials: list[str] = []
    for result in results:
        if result.arm is not Arm.FULL:
            continue
        expected_boundary = expected_boundaries[result.case_id]
        expected_count = 1 if expected_boundary else 0
        if result.task_hash_changed_count != expected_count:
            invalid_trials.append(
                f"{result.case_id}/repetition-{result.repetition} "
                f"expected task_hash_changed={expected_count}, got {result.task_hash_changed_count}"
            )
    if invalid_trials:
        raise BenchmarkValidityError("required full-arm boundary events invalid: " + "; ".join(invalid_trials))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_result(result: TrialResult, path: Path) -> None:
    path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run task-boundary compaction benchmark trials.")
    parser.add_argument("--suite", choices=("controlled", "historical"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--context-window", type=int, required=True)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", default="run")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--max-tool-rounds", type=int, default=_DEFAULT_MAX_TOOL_ROUNDS)
    parser.add_argument("--max-provider-calls", type=int, default=_DEFAULT_MAX_PROVIDER_CALLS)
    parser.add_argument("--max-turn-seconds", type=float, default=_DEFAULT_MAX_TURN_SECONDS)
    parser.add_argument("--provider-timeout-seconds", type=float, default=_DEFAULT_PROVIDER_TIMEOUT_SECONDS)
    return parser.parse_args()


def main() -> None:
    arguments = _parse_arguments()
    fixture_root = Path(__file__).parent / "fixtures"
    cases: Sequence[CaseDefinition]
    if arguments.suite == "controlled":
        cases = load_controlled_cases(fixture_root / "controlled_cases.json")
    else:
        cases = load_historical_cases(
            fixture_root / "historical_cases.json",
            repo_root=arguments.project_root,
        )
    config = RunConfig(
        output_root=arguments.output,
        run_id=arguments.run_id,
        project_root=arguments.project_root,
        model=arguments.model,
        context_window=arguments.context_window,
        max_tool_rounds=arguments.max_tool_rounds,
        max_provider_calls=arguments.max_provider_calls,
        max_turn_seconds=arguments.max_turn_seconds,
        provider_timeout_seconds=arguments.provider_timeout_seconds,
    )
    try:
        results = run_matrix(cases, repetitions=arguments.repetitions, config=config)
    except BenchmarkValidityError as error:
        print(f"benchmark boundary gate failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    print(json.dumps({"trials": len(results), "output": str(arguments.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
