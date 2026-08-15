"""用固定六题、单并发配置运行 Terminal-Bench A/B 样本。"""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from benchmark.harbor.shared.preflight import (
    build_mounts,
    load_env_file,
    read_image_file,
    render_report,
    run_preflight,
)


FIXED_TASKS = (
    "chess-best-move",
    "configure-git-webserver",
    "compile-compcert",
    "qemu-alpine-ssh",
    "adaptive-rejection-sampler",
    "tune-mjcf",
)
DATASET = "terminal-bench@2.0"
AGENT_IMPORT_PATH = "benchmark.harbor.shared.firstcoder_agent:FirstCoderHarborAgent"
PASSTHROUGH_VARIABLES = (
    "FIRSTCODER_PROVIDER",
    "FIRSTCODER_PROVIDER_NAME",
    "FIRSTCODER_MODEL",
    "FIRSTCODER_BASE_URL",
    "FIRSTCODER_API_KEY",
)


def build_harbor_command(
    *,
    harbor_executable: str,
    env_file: str | Path,
    output_dir: str | Path,
    cache_dir: str | Path,
    wheelhouse_dir: str | Path | None,
    provider_name: str,
    model: str,
    max_tool_rounds: int,
    reasoning_effort: str | None,
    wheelhouse_only: bool,
    max_turn_seconds: float = 3300.0,
    agent_timeout_multiplier: float = 4.0,
    tasks: tuple[str, ...] = FIXED_TASKS,
) -> list[str]:
    command = [harbor_executable, "run", "--dataset", DATASET]
    for task in tasks:
        command.extend(["--include-task-name", task])
    command.extend(
        [
            "--agent",
            AGENT_IMPORT_PATH,
            "--model",
            f"{provider_name}/{model}",
            "--n-concurrent",
            "1",
            "--n-attempts",
            "1",
            "--timeout-multiplier",
            "2",
            "--agent-timeout-multiplier",
            _format_number(agent_timeout_multiplier),
            "--agent-setup-timeout-multiplier",
            "3",
            "--agent-kwarg",
            f"max_tool_rounds={max_tool_rounds}",
            "--agent-kwarg",
            f"max_turn_seconds={_format_number(max_turn_seconds)}",
            "--env-file",
            str(Path(env_file)),
        ]
    )
    if reasoning_effort:
        command.extend(["--agent-kwarg", f"reasoning_effort={reasoning_effort}"])
    for name in PASSTHROUGH_VARIABLES:
        value = model if name == "FIRSTCODER_MODEL" else f"${{{name}}}"
        command.extend(["--agent-env", f"{name}={value}"])
    command.extend(
        [
            "--agent-env",
            "FIRSTCODER_DISABLE_GLOBAL_SKILLS=${FIRSTCODER_DISABLE_GLOBAL_SKILLS:-1}",
        ]
    )
    if wheelhouse_only:
        command.extend(["--agent-env", "FIRSTCODER_WHEELHOUSE_ONLY=1"])
    command.extend(
        [
            "--mounts",
            json.dumps(build_mounts(cache_dir, wheelhouse_dir), ensure_ascii=False),
            "--jobs-dir",
            str(Path(output_dir)),
            "--yes",
        ]
    )
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行固定 Terminal-Bench 六题 A/B 样本")
    parser.add_argument("--env-file", type=Path, default=Path(".env.harbor"))
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path.home() / ".cache" / "firstcoder-harbor",
    )
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--wheelhouse-only", action="store_true")
    parser.add_argument(
        "--image-file",
        type=Path,
        default=Path("benchmark/harbor/terminal_bench/terminal-bench-ab-images.txt"),
    )
    parser.add_argument("--pull-images", action="store_true")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--label", default=datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--output-root", type=Path, default=Path("benchmark/runs/harbor"))
    parser.add_argument("--max-tool-rounds", type=int, default=120)
    parser.add_argument("--max-turn-seconds", type=_positive_float, default=3300.0)
    parser.add_argument("--agent-timeout-multiplier", type=_positive_float, default=4.0)
    parser.add_argument(
        "--task",
        action="append",
        choices=FIXED_TASKS,
        dest="tasks",
        help="Run only a selected task from the fixed six-task regression set. Repeat as needed.",
    )
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--model")
    parser.add_argument("--harbor-executable", default=str(Path(".venv/Scripts/harbor.exe")))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    values = load_env_file(args.env_file)
    for name in PASSTHROUGH_VARIABLES:
        if name not in values and os.environ.get(name):
            values[name] = os.environ[name]
    if not args.skip_preflight and not args.dry_run:
        report = run_preflight(
            env_file=args.env_file,
            cache_dir=args.cache_dir,
            model_override=args.model,
            wheelhouse_dir=args.wheelhouse,
            wheelhouse_only=args.wheelhouse_only,
            images=read_image_file(args.image_file),
            pull_images=args.pull_images,
            require_images=args.pull_images,
        )
        print(render_report(report), end="")
        if not report.ok:
            return 1

    output_dir = args.output_root / f"terminal-bench-ab-{args.label}"
    model = args.model or values.get("FIRSTCODER_MODEL", "")
    command = build_harbor_command(
        harbor_executable=args.harbor_executable,
        env_file=args.env_file,
        output_dir=output_dir,
        cache_dir=args.cache_dir,
        wheelhouse_dir=args.wheelhouse,
        provider_name=values.get("FIRSTCODER_PROVIDER_NAME", ""),
        model=model,
        max_tool_rounds=args.max_tool_rounds,
        max_turn_seconds=args.max_turn_seconds,
        agent_timeout_multiplier=args.agent_timeout_multiplier,
        reasoning_effort=args.reasoning_effort,
        wheelhouse_only=args.wheelhouse_only,
        tasks=tuple(args.tasks) if args.tasks else FIXED_TASKS,
    )
    if args.dry_run:
        print(" ".join(shlex.quote(part) for part in command))
        return 0

    process_env = os.environ.copy()
    project_root = str(Path(__file__).resolve().parents[2])
    process_env["PYTHONPATH"] = os.pathsep.join(
        value for value in (project_root, process_env.get("PYTHONPATH")) if value
    )
    return subprocess.run(command, check=False, env=process_env).returncode


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


def _format_number(value: float) -> str:
    return format(value, "g")


if __name__ == "__main__":
    raise SystemExit(main())
