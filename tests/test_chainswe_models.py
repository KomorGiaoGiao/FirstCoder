import json
from pathlib import Path

import pytest

from benchmark.chainswe.models import ChainSWEChain, ChainSWEIssue, load_chains_jsonl, select_chain


def _chain_record(*, continuous_id: str = "chain-1", bug_fixes: list[dict] | None = None) -> dict:
    return {
        "continuous_id": continuous_id,
        "repo": "pallets/flask",
        "base_commit": "abc123",
        "docker_image": "chainswe/flask:latest",
        "bug_fixes": bug_fixes
        if bug_fixes is not None
        else [
            {
                "order": 2,
                "swebench_instance_id": "pallets__flask-2",
                "problem_statement": "Second fix.",
                "FAIL_TO_PASS": ["test_second"],
                "PASS_TO_PASS": [],
            },
            {
                "order": 1,
                "swebench_instance_id": "pallets__flask-1",
                "problem_statement": "First fix.",
                "test_patch": "diff --git a/tests.py b/tests.py",
                "FAIL_TO_PASS": ["test_first"],
                "PASS_TO_PASS": ["test_existing"],
                "test_cmds": "pytest tests",
            },
        ],
    }


def _write_jsonl(path: Path, *records: dict) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_load_chains_jsonl_parses_and_orders_issues(tmp_path: Path):
    path = tmp_path / "chains.jsonl"
    _write_jsonl(path, _chain_record())

    chains = load_chains_jsonl(path)

    assert chains == [
        ChainSWEChain(
            continuous_id="chain-1",
            repo="pallets/flask",
            base_commit="abc123",
            docker_image="chainswe/flask:latest",
            issues=(
                ChainSWEIssue(
                    order=1,
                    swebench_instance_id="pallets__flask-1",
                    problem_statement="First fix.",
                    test_patch="diff --git a/tests.py b/tests.py",
                    fail_to_pass=("test_first",),
                    pass_to_pass=("test_existing",),
                    test_cmds="pytest tests",
                ),
                ChainSWEIssue(
                    order=2,
                    swebench_instance_id="pallets__flask-2",
                    problem_statement="Second fix.",
                    fail_to_pass=("test_second",),
                    pass_to_pass=(),
                ),
            ),
        )
    ]
    assert chains[0].bug_fixes == chains[0].issues


def test_load_chains_jsonl_accepts_json_encoded_test_name_lists(tmp_path: Path):
    path = tmp_path / "chains.jsonl"
    record = _chain_record(
        bug_fixes=[
            {
                "order": 1,
                "swebench_instance_id": "pallets__flask-1",
                "problem_statement": "Fix it.",
                "FAIL_TO_PASS": '["tests/test_a.py::test_a"]',
                "PASS_TO_PASS": '[]',
            }
        ]
    )
    _write_jsonl(path, record)

    issue = load_chains_jsonl(path)[0].issues[0]

    assert issue.fail_to_pass == ("tests/test_a.py::test_a",)
    assert issue.pass_to_pass == ()


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (_chain_record(bug_fixes=[]), "bug_fixes must be a non-empty list"),
        (
            _chain_record(
                bug_fixes=[
                    {
                        "order": 1,
                        "swebench_instance_id": "pallets__flask-1",
                        "problem_statement": "Fix it.",
                        "FAIL_TO_PASS": "not JSON",
                        "PASS_TO_PASS": [],
                    }
                ]
            ),
            "FAIL_TO_PASS must be a list of strings",
        ),
        (
            _chain_record(
                bug_fixes=[
                    {
                        "order": 0,
                        "swebench_instance_id": "pallets__flask-1",
                        "problem_statement": "Fix it.",
                        "FAIL_TO_PASS": [],
                        "PASS_TO_PASS": [],
                    }
                ]
            ),
            "order must be a positive integer",
        ),
    ],
)
def test_load_chains_jsonl_rejects_invalid_records(tmp_path: Path, record: dict, message: str):
    path = tmp_path / "chains.jsonl"
    _write_jsonl(path, record)

    with pytest.raises(ValueError, match=message):
        load_chains_jsonl(path)


def test_load_chains_jsonl_rejects_missing_required_field(tmp_path: Path):
    path = tmp_path / "chains.jsonl"
    record = _chain_record()
    del record["docker_image"]
    _write_jsonl(path, record)

    with pytest.raises(ValueError, match="missing required field: docker_image"):
        load_chains_jsonl(path)


def test_load_chains_jsonl_rejects_duplicate_order_and_continuous_id(tmp_path: Path):
    path = tmp_path / "chains.jsonl"
    duplicate_order = _chain_record(
        bug_fixes=[
            {
                "order": 1,
                "swebench_instance_id": "pallets__flask-1",
                "problem_statement": "First.",
                "FAIL_TO_PASS": [],
                "PASS_TO_PASS": [],
            },
            {
                "order": 1,
                "swebench_instance_id": "pallets__flask-2",
                "problem_statement": "Second.",
                "FAIL_TO_PASS": [],
                "PASS_TO_PASS": [],
            },
        ]
    )
    _write_jsonl(path, duplicate_order)

    with pytest.raises(ValueError, match="duplicate order"):
        load_chains_jsonl(path)

    _write_jsonl(path, _chain_record(), _chain_record())
    with pytest.raises(ValueError, match="duplicate continuous_id"):
        load_chains_jsonl(path)


def test_select_chain_requires_exactly_one_matching_id():
    first = ChainSWEChain(
        continuous_id="first",
        repo="org/repo",
        base_commit="abc",
        docker_image="image",
        issues=(
            ChainSWEIssue(
                order=1,
                swebench_instance_id="org__repo-1",
                problem_statement="Fix it.",
                fail_to_pass=(),
                pass_to_pass=(),
            ),
        ),
    )
    duplicate = ChainSWEChain(
        continuous_id="first",
        repo="org/repo",
        base_commit="def",
        docker_image="image",
        issues=first.issues,
    )

    assert select_chain([first], "first") is first
    with pytest.raises(ValueError, match="unknown continuous_id"):
        select_chain([first], "missing")
    with pytest.raises(ValueError, match="duplicate continuous_id"):
        select_chain([first, duplicate], "first")
