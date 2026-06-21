import json
import subprocess
from pathlib import Path

from firstcoder.eval.swebench import load_instances_jsonl, run_instances, write_predictions_jsonl
from firstcoder.eval.tasks import CodingTask, CodingTaskResult


class FakeAdapter:
    def run_task(self, task: CodingTask) -> CodingTaskResult:
        (task.repo_path / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
        diff = subprocess.run(
            ["git", "diff", "--binary"],
            cwd=task.repo_path,
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        return CodingTaskResult(
            instance_id=task.instance_id,
            model_name_or_path="firstcoder-fake",
            model_patch=diff,
        )


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def test_local_prediction_generation_smoke(tmp_path: Path):
    repos_root = tmp_path / "repos"
    repo = repos_root / "local__sample-1"
    repo.mkdir(parents=True)
    run(["git", "init"], repo)
    run(["git", "config", "user.email", "test@example.com"], repo)
    run(["git", "config", "user.name", "Test User"], repo)
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    run(["git", "add", "module.py"], repo)
    run(["git", "commit", "-m", "init"], repo)

    instances_path = tmp_path / "instances.jsonl"
    instances_path.write_text(
        json.dumps(
            {
                "instance_id": "local__sample-1",
                "repo": "local/sample",
                "base_commit": "HEAD",
                "problem_statement": "Change VALUE from 1 to 2 in module.py.",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    results = run_instances(
        instances=load_instances_jsonl(instances_path),
        repos_root=repos_root,
        adapter=FakeAdapter(),
        max_instances=1,
    )
    out = tmp_path / "predictions.jsonl"
    write_predictions_jsonl(out, results)

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["instance_id"] == "local__sample-1"
    assert payload["model_name_or_path"] == "firstcoder-fake"
    assert "diff --git a/module.py b/module.py" in payload["model_patch"]
