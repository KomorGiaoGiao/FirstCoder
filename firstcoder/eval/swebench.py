"""SWE-bench Lite prediction generation for FirstCoder."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from firstcoder.eval.adapter import CodingAgentAdapter, FirstCoderCodingAgentAdapter
from firstcoder.eval.patch import ensure_clean_repo
from firstcoder.eval.tasks import CodingTask, CodingTaskResult


@dataclass(frozen=True, slots=True)
class SwebenchInstance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str


def load_instances_jsonl(path: str | Path) -> list[SwebenchInstance]:
    instances: list[SwebenchInstance] = []
    with Path(path).open("r", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            data = json.loads(line)
            instances.append(
                SwebenchInstance(
                    instance_id=str(data["instance_id"]),
                    repo=str(data["repo"]),
                    base_commit=str(data["base_commit"]),
                    problem_statement=str(data["problem_statement"]),
                )
            )
    return instances


def repo_path_for_instance(repos_root: str | Path, instance: SwebenchInstance) -> Path:
    return Path(repos_root) / instance.instance_id


def write_predictions_jsonl(path: str | Path, results: Iterable[CodingTaskResult]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as file:
        for result in results:
            file.write(json.dumps(result.to_prediction_dict(), ensure_ascii=False, separators=(",", ":")))
            file.write("\n")


def build_harness_command(
    *,
    predictions_path: str | Path,
    run_id: str,
    max_workers: int = 1,
    dataset_name: str = "princeton-nlp/SWE-bench_Lite",
) -> list[str]:
    return [
        "python",
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        dataset_name,
        "--predictions_path",
        str(predictions_path),
        "--max_workers",
        str(max_workers),
        "--run_id",
        run_id,
    ]


def run_instances(
    *,
    instances: list[SwebenchInstance],
    repos_root: str | Path,
    adapter: CodingAgentAdapter,
    max_instances: int | None = None,
) -> list[CodingTaskResult]:
    selected = instances[:max_instances] if max_instances is not None else instances
    results: list[CodingTaskResult] = []
    for instance in selected:
        repo_path = repo_path_for_instance(repos_root, instance)
        ensure_clean_repo(repo_path)
        task = CodingTask(
            instance_id=instance.instance_id,
            repo_path=repo_path,
            problem_statement=instance.problem_statement,
            base_commit=instance.base_commit,
            metadata={"repo": instance.repo},
        )
        results.append(adapter.run_task(task))
    return results


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate SWE-bench Lite predictions with FirstCoder.")
    parser.add_argument("--instances", required=True, help="Path to SWE-bench style instances JSONL.")
    parser.add_argument("--repos-root", required=True, help="Directory containing one repo per instance_id.")
    parser.add_argument("--out", required=True, help="Output predictions JSONL path.")
    parser.add_argument("--provider", default=None, help="FirstCoder provider name. Defaults to app config.")
    parser.add_argument("--model-name", default="firstcoder", help="Value for model_name_or_path.")
    parser.add_argument("--session-root", default=".firstcoder-eval", help="Directory for benchmark session logs.")
    parser.add_argument("--max-instances", type=_positive_int, default=1, help="Maximum instances to run.")
    parser.add_argument(
        "--print-harness-command",
        action="store_true",
        help="Print the official SWE-bench evaluation command after writing predictions.",
    )
    parser.add_argument("--run-id", default="firstcoder-swe-lite", help="Run id for the official SWE-bench harness command.")
    parser.add_argument("--max-workers", type=_positive_int, default=1, help="Worker count for the official SWE-bench harness command.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    adapter = FirstCoderCodingAgentAdapter(
        model_name_or_path=args.model_name,
        provider_name=args.provider,
        session_root=args.session_root,
    )
    results = run_instances(
        instances=load_instances_jsonl(args.instances),
        repos_root=args.repos_root,
        adapter=adapter,
        max_instances=args.max_instances,
    )
    write_predictions_jsonl(args.out, results)
    if args.print_harness_command:
        print(" ".join(build_harness_command(predictions_path=args.out, run_id=args.run_id, max_workers=args.max_workers)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
