import json
from pathlib import Path

import pytest

from benchmark.evalplus.runner import (
    EvalPlusTask,
    _strip_markdown_fence,
    build_parser,
    evaluate_samples,
    generate_samples,
    load_tasks,
)
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse


class FakeProvider(ChatProvider):
    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        return ChatResponse(
            provider=self.name,
            model=self.model,
            content=(
                "```python\n"
                "def add_one(x):\n"
                "    return x + 1\n"
                "```"
            ),
        )


def test_parser_supports_small_humaneval_mini_run():
    args = build_parser().parse_args(
        [
            "generate",
            "--dataset",
            "humaneval",
            "--mini",
            "--id-range",
            "0",
            "3",
            "--samples-out",
            "runs/evalplus/humaneval.jsonl",
        ]
    )

    assert args.dataset == "humaneval"
    assert args.command == "generate"
    assert args.mini is True
    assert args.id_range == [0, 3]


def test_parser_supports_generate_subcommand():
    args = build_parser().parse_args(
        [
            "generate",
            "--dataset",
            "humaneval",
            "--samples-out",
            "runs/evalplus/humaneval.jsonl",
        ]
    )

    assert args.command == "generate"
    assert args.dataset == "humaneval"


def test_load_tasks_filters_by_numeric_id_range():
    tasks = load_tasks("humaneval", mini=True, id_range=(0, 2))

    assert [task.task_id for task in tasks] == ["HumanEval/0", "HumanEval/1"]


def test_load_tasks_rejects_mbpp_mini():
    with pytest.raises(ValueError, match="MBPP"):
        load_tasks("mbpp", mini=True)


def test_generate_samples_writes_evalplus_jsonl_and_summary(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("benchmark.evalplus.runner.create_provider", lambda provider_name=None: FakeProvider())
    samples = tmp_path / "samples.jsonl"
    summary = tmp_path / "summary.json"

    rows = generate_samples(
        dataset="humaneval",
        tasks=[EvalPlusTask(task_id="HumanEval/999", prompt="def add_one(x):\n", entry_point="add_one")],
        samples_out=samples,
        summary_out=summary,
    )

    written = [json.loads(line) for line in samples.read_text(encoding="utf-8").splitlines()]
    assert rows == written
    assert written[0]["task_id"] == "HumanEval/999"
    assert "def add_one" in written[0]["solution"]
    assert json.loads(summary.read_text(encoding="utf-8"))["sample_count"] == 1


def test_evaluate_samples_scores_subset(tmp_path: Path):
    samples = tmp_path / "samples.jsonl"
    samples.write_text(
        json.dumps(
            {
                "task_id": "HumanEval/0",
                "solution": (
                    "from typing import List\n\n"
                    "def has_close_elements(numbers: List[float], threshold: float) -> bool:\n"
                    "    for i, first in enumerate(numbers):\n"
                    "        for second in numbers[i + 1:]:\n"
                    "            if abs(first - second) < threshold:\n"
                    "                return True\n"
                    "    return False\n"
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    summary = tmp_path / "eval.json"

    payload = evaluate_samples(
        dataset="humaneval",
        samples=samples,
        summary_out=summary,
        mini=True,
        id_range=(0, 1),
    )

    assert payload["base_passed"] == 1
    assert payload["plus_passed"] == 1
    assert json.loads(summary.read_text(encoding="utf-8"))["plus_passed"] == 1


def test_evaluate_samples_defaults_to_tasks_present_in_samples(tmp_path: Path):
    samples = tmp_path / "samples.jsonl"
    samples.write_text(
        json.dumps(
            {
                "task_id": "HumanEval/0",
                "solution": (
                    "from typing import List\n\n"
                    "def has_close_elements(numbers: List[float], threshold: float) -> bool:\n"
                    "    return any(abs(a - b) < threshold for i, a in enumerate(numbers) for b in numbers[i + 1:])\n"
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    payload = evaluate_samples(
        dataset="humaneval",
        samples=samples,
        summary_out=tmp_path / "summary.json",
        mini=True,
    )

    assert payload["sample_count"] == 1


def test_generate_samples_requires_force_for_existing_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("benchmark.evalplus.runner.create_provider", lambda provider_name=None: FakeProvider())
    samples = tmp_path / "samples.jsonl"
    samples.write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Use --force"):
        generate_samples(
            dataset="humaneval",
            tasks=[EvalPlusTask(task_id="HumanEval/999", prompt="def add_one(x):\n", entry_point="add_one")],
            samples_out=samples,
            summary_out=tmp_path / "summary.json",
        )


def test_strip_markdown_fence_extracts_code():
    assert _strip_markdown_fence("```python\nVALUE = 1\n```") == "VALUE = 1"
