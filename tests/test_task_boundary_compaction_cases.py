from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from benchmark.task_boundary_compaction.cases import (
    load_aider_chain_cases,
    materialize_aider_batch,
    materialize_aider_task,
)


def _write_aider_task(root: Path, task_id: str) -> Path:
    task_root = root / task_id
    workspace = task_root / "environment" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "Subject.java").write_text(f"class {task_id.replace('-', '_')} {{}}\n", encoding="utf-8")
    tests = task_root / "tests"
    tests.mkdir()
    (tests / "test.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (task_root / "environment" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (task_root / "instruction.md").write_text(
        f"# Instructions\n\nImplement {task_id}.\n",
        encoding="utf-8",
    )
    return task_root


def _write_manifest(root: Path, cases: list[dict[str, object]]) -> Path:
    path = root / "aider-chains.json"
    path.write_text(json.dumps({"suite": "aider-chain", "cases": cases}), encoding="utf-8")
    return path


def test_load_aider_chain_cases_builds_real_a_then_independent_b_chain(tmp_path: Path) -> None:
    aider_root = tmp_path / "aider"
    for task_id in ("task-a", "task-b", "task-c", "task-d"):
        _write_aider_task(aider_root, task_id)
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "case_id": "batch-a-to-b",
                "chain_type": "batch",
                "a_task_ids": ["task-a", "task-c", "task-d"],
                "b_task_id": "task-b",
            }
        ],
    )

    chain = load_aider_chain_cases(manifest, aider_root=aider_root)[0]

    assert chain.case_id == "batch-a-to-b"
    assert chain.chain_type == "batch"
    assert [task.task_id for task in chain.a_tasks] == ["task-a", "task-c", "task-d"]
    assert chain.b_task.task_id == "task-b"
    assert len(chain.a_turns) == 9
    assert chain.benchmark_case.kind == "aider_chain"
    assert [turn.expected_decision for turn in chain.benchmark_case.turns] == ["new", "same"]
    assert "Implement task-b" in chain.benchmark_case.turns[0].message


def test_materializers_copy_workspace_without_touching_aider_source(tmp_path: Path) -> None:
    aider_root = tmp_path / "aider"
    task_a = _write_aider_task(aider_root, "task-a")
    _write_aider_task(aider_root, "task-b")
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "case_id": "natural-a-to-b",
                "chain_type": "natural",
                "a_task_ids": ["task-a"],
                "b_task_id": "task-b",
            }
        ],
    )
    chain = load_aider_chain_cases(manifest, aider_root=aider_root)[0]

    b_destination = tmp_path / "b-project"
    materialize_aider_task(chain.b_task, destination=b_destination)
    a_destination = tmp_path / "a-batch"
    materialized = materialize_aider_batch(chain, destination=a_destination)

    assert (b_destination / "Subject.java").read_text(encoding="utf-8").startswith("class task_b")
    assert (a_destination / "01-task-a" / "Subject.java").exists()
    assert materialized == (("task-a", a_destination / "01-task-a"),)
    assert (task_a / "environment" / "workspace" / "Subject.java").exists()
    for project_root in (a_destination, b_destination):
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        )
        assert status.stdout == ""


def test_load_aider_chain_cases_rejects_task_reuse_and_invalid_layout(tmp_path: Path) -> None:
    aider_root = tmp_path / "aider"
    _write_aider_task(aider_root, "task-a")
    _write_aider_task(aider_root, "task-b")

    reused = _write_manifest(
        tmp_path,
        [
            {
                "case_id": "one",
                "chain_type": "natural",
                "a_task_ids": ["task-a"],
                "b_task_id": "task-b",
            },
            {
                "case_id": "two",
                "chain_type": "natural",
                "a_task_ids": ["task-b"],
                "b_task_id": "task-a",
            },
        ],
    )
    with pytest.raises(ValueError, match="reused Aider task"):
        load_aider_chain_cases(reused, aider_root=aider_root)

    invalid = _write_manifest(
        tmp_path,
        [
            {
                "case_id": "bad-natural",
                "chain_type": "natural",
                "a_task_ids": ["task-a", "task-b"],
                "b_task_id": "task-b",
            }
        ],
    )
    with pytest.raises(ValueError, match="natural"):
        load_aider_chain_cases(invalid, aider_root=aider_root)
