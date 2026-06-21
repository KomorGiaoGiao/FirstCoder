from pathlib import Path

from firstcoder.eval.tasks import CodingTask, CodingTaskResult


def test_coding_task_exposes_prompt_inputs(tmp_path: Path):
    task = CodingTask(
        instance_id="sympy__sympy-20590",
        repo_path=tmp_path,
        problem_statement="Fix sympify error handling.",
        base_commit="abc123",
        metadata={"repo": "sympy/sympy"},
    )

    assert task.instance_id == "sympy__sympy-20590"
    assert task.repo_path == tmp_path
    assert task.metadata["repo"] == "sympy/sympy"


def test_coding_task_result_serializes_to_swebench_prediction():
    result = CodingTaskResult(
        instance_id="sympy__sympy-20590",
        model_name_or_path="firstcoder",
        model_patch="diff --git a/a.py b/a.py\n",
        transcript_path=Path("/tmp/session.jsonl"),
    )

    assert result.to_prediction_dict() == {
        "instance_id": "sympy__sympy-20590",
        "model_name_or_path": "firstcoder",
        "model_patch": "diff --git a/a.py b/a.py\n",
    }
