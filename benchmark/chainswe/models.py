"""Strict, dependency-free models for ChainSWE JSONL datasets."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


@dataclass(frozen=True, slots=True)
class ChainSWEIssue:
    """One ordered SWE-bench issue in a continuous ChainSWE task."""

    order: int
    swebench_instance_id: str
    problem_statement: str
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    test_patch: str | None = None
    test_cmds: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.order, int) or isinstance(self.order, bool) or self.order < 1:
            raise ValueError("order must be a positive integer")
        _require_nonempty_string(self.swebench_instance_id, "swebench_instance_id")
        _require_nonempty_string(self.problem_statement, "problem_statement")
        object.__setattr__(self, "fail_to_pass", _normalise_string_tuple(self.fail_to_pass, "fail_to_pass"))
        object.__setattr__(self, "pass_to_pass", _normalise_string_tuple(self.pass_to_pass, "pass_to_pass"))
        _require_optional_string(self.test_patch, "test_patch")
        _require_optional_string(self.test_cmds, "test_cmds")


@dataclass(frozen=True, slots=True)
class ChainSWEChain:
    """A repository and its ordered sequence of ChainSWE issues."""

    continuous_id: str
    repo: str
    base_commit: str
    docker_image: str
    issues: tuple[ChainSWEIssue, ...]

    def __post_init__(self) -> None:
        _require_nonempty_string(self.continuous_id, "continuous_id")
        _require_nonempty_string(self.repo, "repo")
        _require_nonempty_string(self.base_commit, "base_commit")
        _require_nonempty_string(self.docker_image, "docker_image")

        if not isinstance(self.issues, (tuple, list)) or not self.issues:
            raise ValueError("issues must be a non-empty sequence of ChainSWEIssue values")
        if not all(isinstance(issue, ChainSWEIssue) for issue in self.issues):
            raise ValueError("issues must contain only ChainSWEIssue values")

        sorted_issues = tuple(sorted(self.issues, key=lambda issue: issue.order))
        orders = [issue.order for issue in sorted_issues]
        if len(orders) != len(set(orders)):
            raise ValueError("issues must not contain duplicate order values")
        object.__setattr__(self, "issues", sorted_issues)

    @property
    def bug_fixes(self) -> tuple[ChainSWEIssue, ...]:
        """Alias for the source dataset's ``bug_fixes`` field."""
        return self.issues


def load_chains_jsonl(path: str | Path) -> list[ChainSWEChain]:
    """Read ChainSWE JSONL records and reject malformed or duplicate chains."""
    chains: list[ChainSWEChain] = []
    seen_ids: set[str] = set()
    with Path(path).open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {line_number}: invalid JSON") from exc

            try:
                chain = parse_chain_record(record)
            except ValueError as exc:
                raise ValueError(f"line {line_number}: {exc}") from exc

            if chain.continuous_id in seen_ids:
                raise ValueError(f"line {line_number}: duplicate continuous_id: {chain.continuous_id}")
            seen_ids.add(chain.continuous_id)
            chains.append(chain)
    return chains


def parse_chain_record(record: Any) -> ChainSWEChain:
    """Parse one official ChainSWE record without reading a dataset file."""

    data = _require_mapping(record, "chain")
    raw_issues = _require_field(data, "bug_fixes")
    if not isinstance(raw_issues, list) or not raw_issues:
        raise ValueError("bug_fixes must be a non-empty list")
    issues = tuple(_parse_issue(raw_issue, index) for index, raw_issue in enumerate(raw_issues))
    return ChainSWEChain(
        continuous_id=_require_nonempty_string(_require_field(data, "continuous_id"), "continuous_id"),
        repo=_require_nonempty_string(_require_field(data, "repo"), "repo"),
        base_commit=_require_nonempty_string(_require_field(data, "base_commit"), "base_commit"),
        docker_image=_require_nonempty_string(_require_field(data, "docker_image"), "docker_image"),
        issues=issues,
    )


def chain_to_record(chain: ChainSWEChain) -> dict[str, Any]:
    """Serialize a selected chain in the official dataset record shape."""

    return {
        "continuous_id": chain.continuous_id,
        "repo": chain.repo,
        "base_commit": chain.base_commit,
        "docker_image": chain.docker_image,
        "bug_fixes": [
            {
                "order": issue.order,
                "swebench_instance_id": issue.swebench_instance_id,
                "problem_statement": issue.problem_statement,
                "FAIL_TO_PASS": list(issue.fail_to_pass),
                "PASS_TO_PASS": list(issue.pass_to_pass),
                "test_patch": issue.test_patch,
                "test_cmds": issue.test_cmds,
            }
            for issue in chain.issues
        ],
    }


def select_chain(chains: Iterable[ChainSWEChain], continuous_id: str) -> ChainSWEChain:
    """Select exactly one chain by id, detecting missing and duplicate selections."""
    _require_nonempty_string(continuous_id, "continuous_id")
    matches = [chain for chain in chains if chain.continuous_id == continuous_id]
    if not matches:
        raise ValueError(f"unknown continuous_id: {continuous_id}")
    if len(matches) > 1:
        raise ValueError(f"duplicate continuous_id: {continuous_id}")
    return matches[0]


def _parse_issue(record: Any, index: int) -> ChainSWEIssue:
    data = _require_mapping(record, f"bug_fixes[{index}]")
    return ChainSWEIssue(
        order=_parse_order(_require_field(data, "order")),
        swebench_instance_id=_require_nonempty_string(
            _require_field(data, "swebench_instance_id"), "swebench_instance_id"
        ),
        problem_statement=_require_nonempty_string(
            _require_field(data, "problem_statement"), "problem_statement"
        ),
        test_patch=_parse_optional_string(data, "test_patch"),
        fail_to_pass=_parse_test_names(_require_field(data, "FAIL_TO_PASS"), "FAIL_TO_PASS"),
        pass_to_pass=_parse_test_names(_require_field(data, "PASS_TO_PASS"), "PASS_TO_PASS"),
        test_cmds=_parse_optional_string(data, "test_cmds"),
    )


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _require_field(data: Mapping[str, Any], name: str) -> Any:
    if name not in data:
        raise ValueError(f"missing required field: {name}")
    return data[name]


def _require_nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _require_optional_string(value: Any, name: str) -> None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")


def _parse_optional_string(data: Mapping[str, Any], name: str) -> str | None:
    value = data.get(name)
    _require_optional_string(value, name)
    return value


def _parse_order(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("order must be a positive integer")
    return value


def _parse_test_names(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name} must be a list of strings or a JSON-encoded list of strings") from exc
    return _normalise_string_tuple(value, name)


def _normalise_string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of strings")
    return tuple(value)
