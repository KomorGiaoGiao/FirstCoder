"""Generate EvalPlus-compatible samples with FirstCoder providers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evalplus.data import get_human_eval_plus, get_mbpp_plus
from evalplus.evaluate import check_correctness, get_groundtruth
from evalplus.eval import PASS
from evalplus.eval._special_oracle import MBPP_OUTPUT_NOT_NONE_TASKS
from evalplus.sanitize import sanitize

from firstcoder.providers.factory import create_provider
from firstcoder.providers.types import ChatMessage, ChatRequest


SUPPORTED_DATASETS = {"humaneval", "mbpp"}


@dataclass(frozen=True, slots=True)
class EvalPlusTask:
    task_id: str
    prompt: str
    entry_point: str


def load_tasks(dataset: str, *, mini: bool = False, id_range: tuple[int, int] | None = None) -> list[EvalPlusTask]:
    if dataset == "humaneval":
        raw_tasks = get_human_eval_plus(mini=mini)
    elif dataset == "mbpp":
        if mini:
            raise ValueError("EvalPlus does not provide an MBPP+ mini split. Use --id-range for small MBPP runs.")
        raw_tasks = get_mbpp_plus()
    else:
        raise ValueError(f"Unsupported dataset: {dataset}")

    tasks: list[EvalPlusTask] = []
    for task_id, task in raw_tasks.items():
        if id_range is not None and not _task_id_in_range(task_id, id_range):
            continue
        tasks.append(EvalPlusTask(task_id=task_id, prompt=task["prompt"], entry_point=task["entry_point"]))
    return tasks


def generate_samples(
    *,
    dataset: str,
    tasks: list[EvalPlusTask],
    samples_out: str | Path,
    summary_out: str | Path,
    provider_name: str | None = None,
    max_tasks: int | None = None,
    temperature: float = 0.0,
    max_tokens: int = 2048,
    force: bool = False,
) -> list[dict[str, Any]]:
    selected = tasks[:max_tasks] if max_tasks is not None else tasks
    out = Path(samples_out)
    if out.exists() and not force:
        raise RuntimeError(f"Samples file already exists: {out}. Use --force to overwrite it.")
    out.parent.mkdir(parents=True, exist_ok=True)
    provider = create_provider(provider_name)

    rows: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    started_all = time.time()
    with out.open("w", encoding="utf-8") as file:
        for task in selected:
            started = time.time()
            response = provider.complete(
                ChatRequest(
                    messages=[
                        ChatMessage(
                            role="system",
                            content=(
                                "You solve Python programming benchmark tasks. "
                                "Return only a complete Python solution; do not use markdown."
                            ),
                        ),
                        ChatMessage(role="user", content=_build_prompt(dataset, task)),
                    ],
                    tools=[],
                    tool_choice="none",
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            )
            raw_solution = _strip_markdown_fence(response.content)
            solution = sanitize(task.prompt + "\n" + raw_solution, entrypoint=task.entry_point)
            row = {"task_id": task.task_id, "solution": solution}
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
            file.flush()
            rows.append(row)
            summary.append(
                {
                    "task_id": task.task_id,
                    "entry_point": task.entry_point,
                    "elapsed_seconds": round(time.time() - started, 3),
                    "provider": response.provider,
                    "model": response.model,
                    "raw_response_chars": len(response.content),
                    "solution_chars": len(solution),
                }
            )

    write_summary_json(
        summary_out,
        {
            "dataset": dataset,
            "sample_count": len(rows),
            "elapsed_seconds": round(time.time() - started_all, 3),
            "samples_out": str(out),
            "tasks": summary,
        },
    )
    return rows


def evaluate_samples(
    *,
    dataset: str,
    samples: str | Path,
    summary_out: str | Path,
    mini: bool = False,
    id_range: tuple[int, int] | None = None,
    base_only: bool = False,
    test_details: bool = False,
    min_time_limit: float = 1.0,
    gt_time_limit_factor: float = 4.0,
    disable_memory_limit: bool = True,
) -> dict[str, Any]:
    if disable_memory_limit:
        os.environ.setdefault("EVALPLUS_MAX_MEMORY_BYTES", "-1")
    raw_tasks = _load_raw_tasks(dataset, mini=mini)
    sample_rows = _load_sample_rows(samples)
    sample_task_ids = {str(row["task_id"]) for row in sample_rows}
    tasks = {
        task_id: task
        for task_id, task in raw_tasks.items()
        if (id_range is None and task_id in sample_task_ids)
        or (id_range is not None and _task_id_in_range(task_id, id_range))
    }
    selected_samples = [row for row in sample_rows if row["task_id"] in tasks]
    if id_range is not None:
        missing = sorted(set(tasks) - {row["task_id"] for row in selected_samples})
        if missing:
            raise RuntimeError(f"Missing samples for selected tasks: {', '.join(missing[:5])}")
    if not selected_samples:
        raise RuntimeError("No samples matched the selected EvalPlus tasks.")

    dataset_hash = _subset_hash(dataset, tasks.keys(), mini=mini)
    expected_output = get_groundtruth(
        tasks,
        dataset_hash,
        MBPP_OUTPUT_NOT_NONE_TASKS if dataset == "mbpp" else [],
    )
    results: list[dict[str, Any]] = []
    for completion_id, sample in enumerate(selected_samples):
        task_id = sample["task_id"]
        solution = sample.get("solution")
        if not solution:
            solution = tasks[task_id]["prompt"] + sample["completion"]
        result = check_correctness(
            dataset,
            completion_id,
            tasks[task_id],
            solution,
            expected_output[task_id],
            base_only=base_only,
            fast_check=not test_details,
            identifier=sample.get("_identifier", f"{task_id}:{completion_id}"),
            min_time_limit=min_time_limit,
            gt_time_limit_factor=gt_time_limit_factor,
        )
        base_status, _base_details = result["base"]
        plus_status = None
        if not base_only:
            plus_status, _plus_details = result["plus"]
        results.append(
            {
                "task_id": task_id,
                "base_status": base_status,
                "plus_status": plus_status,
                "passed_base": base_status == PASS,
                "passed_plus": plus_status == PASS if plus_status is not None else None,
            }
        )

    base_passed = sum(1 for row in results if row["passed_base"])
    plus_passed = sum(1 for row in results if row["passed_plus"])
    payload = {
        "dataset": dataset,
        "sample_count": len(results),
        "base_passed": base_passed,
        "base_pass_rate": round(base_passed / len(results), 4) if results else 0.0,
        "plus_passed": plus_passed if not base_only else None,
        "plus_pass_rate": round(plus_passed / len(results), 4) if results and not base_only else None,
        "samples": str(samples),
        "results": results,
    }
    write_summary_json(summary_out, payload)
    return payload


def write_summary_json(path: str | Path, payload: dict[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate EvalPlus-compatible samples with FirstCoder providers.")
    subparsers = parser.add_subparsers(dest="command")
    _add_generate_args(subparsers.add_parser("generate", help="Generate EvalPlus samples JSONL."))
    evaluate_parser = subparsers.add_parser("evaluate", help="Evaluate a subset of EvalPlus samples.")
    evaluate_parser.add_argument("--dataset", choices=sorted(SUPPORTED_DATASETS), required=True)
    evaluate_parser.add_argument("--samples", required=True, help="EvalPlus samples JSONL path.")
    evaluate_parser.add_argument("--summary-out", default="runs/evalplus-eval-summary.json", help="Evaluation summary JSON path.")
    evaluate_parser.add_argument("--id-range", nargs=2, type=int, metavar=("LOW", "HIGH"), help="Evaluate tasks with numeric ids in [LOW, HIGH).")
    evaluate_parser.add_argument("--mini", action="store_true", help="Use EvalPlus mini split when available.")
    evaluate_parser.add_argument("--base-only", action="store_true", help="Evaluate only base tests.")
    evaluate_parser.add_argument("--test-details", action="store_true", help="Run detailed checks instead of fast failing checks.")
    evaluate_parser.add_argument("--keep-memory-limit", action="store_true", help="Keep EvalPlus memory guard enabled.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "generate"
    if command == "generate":
        id_range = tuple(args.id_range) if args.id_range is not None else None
        tasks = load_tasks(args.dataset, mini=args.mini, id_range=id_range)
        if not tasks:
            raise SystemExit("No EvalPlus tasks selected.")
        generate_samples(
            dataset=args.dataset,
            tasks=tasks,
            samples_out=args.samples_out,
            summary_out=args.summary_out,
            provider_name=args.provider,
            max_tasks=args.max_tasks,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            force=args.force,
        )
    elif command == "evaluate":
        id_range = tuple(args.id_range) if args.id_range is not None else None
        summary = evaluate_samples(
            dataset=args.dataset,
            samples=args.samples,
            summary_out=args.summary_out,
            mini=args.mini,
            id_range=id_range,
            base_only=args.base_only,
            test_details=args.test_details,
            disable_memory_limit=not args.keep_memory_limit,
        )
        print(
            f"{summary['dataset']}: base {summary['base_passed']}/{summary['sample_count']}"
            + (
                f", plus {summary['plus_passed']}/{summary['sample_count']}"
                if summary["plus_passed"] is not None
                else ""
            )
        )
    return 0


def _add_generate_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset", choices=sorted(SUPPORTED_DATASETS), required=True)
    parser.add_argument("--samples-out", required=True, help="Output EvalPlus samples JSONL path.")
    parser.add_argument("--summary-out", default="runs/evalplus-summary.json", help="Generation summary JSON path.")
    parser.add_argument("--provider", default=None, help="FirstCoder provider name. Defaults to app config.")
    parser.add_argument("--max-tasks", type=_positive_int, default=None, help="Limit number of selected tasks.")
    parser.add_argument("--id-range", nargs=2, type=int, metavar=("LOW", "HIGH"), help="Run tasks with numeric ids in [LOW, HIGH).")
    parser.add_argument("--mini", action="store_true", help="Use EvalPlus mini split when available.")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=_positive_int, default=2048)
    parser.add_argument("--force", action="store_true", help="Overwrite existing samples file.")


def _build_prompt(dataset: str, task: EvalPlusTask) -> str:
    return (
        f"Dataset: {dataset}\n"
        f"Task: {task.task_id}\n\n"
        "Complete the following Python function. Keep the function name and signature unchanged.\n"
        "Return a self-contained Python file that includes the prompt code and your implementation.\n\n"
        f"{task.prompt.rstrip()}\n"
    )


def _load_raw_tasks(dataset: str, *, mini: bool) -> dict[str, dict[str, Any]]:
    if dataset == "humaneval":
        return get_human_eval_plus(mini=mini)
    if dataset == "mbpp":
        if mini:
            raise ValueError("EvalPlus does not provide an MBPP+ mini split. Use --id-range for small MBPP runs.")
        return get_mbpp_plus()
    raise ValueError(f"Unsupported dataset: {dataset}")


def _dataset_hash(dataset: str, *, mini: bool) -> str:
    if dataset == "humaneval":
        from evalplus.data import get_human_eval_plus_hash

        return get_human_eval_plus_hash(mini=mini)
    if dataset == "mbpp":
        from evalplus.data import get_mbpp_plus_hash

        if mini:
            raise ValueError("EvalPlus does not provide an MBPP+ mini split.")
        return get_mbpp_plus_hash()
    raise ValueError(f"Unsupported dataset: {dataset}")


def _subset_hash(dataset: str, task_ids: Iterable[str], *, mini: bool) -> str:
    suffix = ",".join(sorted(task_ids))
    digest = hashlib.sha256(suffix.encode("utf-8")).hexdigest()[:16]
    return f"{_dataset_hash(dataset, mini=mini)}-{digest}"


def _load_sample_rows(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _task_id_in_range(task_id: str, id_range: tuple[int, int]) -> bool:
    low, high = id_range
    if low >= high:
        raise ValueError("--id-range LOW must be smaller than HIGH")
    match = re.search(r"/(\d+)$", task_id)
    if match is None:
        return False
    number = int(match.group(1))
    return low <= number < high


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _strip_markdown_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```(?:python|py)?\s*\n(.*)\n```", stripped, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else stripped


if __name__ == "__main__":
    raise SystemExit(main())
