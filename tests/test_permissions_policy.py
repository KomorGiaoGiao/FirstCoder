from pathlib import Path

from firstcoder.permissions.policy import DefaultPermissionPolicy
from firstcoder.permissions.types import (
    PermissionAction,
    PermissionDecisionKind,
    PermissionMode,
    PermissionRequest,
)


def _request(action: PermissionAction, target: str, *, cwd: Path | None = None) -> PermissionRequest:
    return PermissionRequest(id="req_1", action=action, target=target, cwd=cwd)


def test_standard_allows_project_root_read(tmp_path) -> None:
    policy = DefaultPermissionPolicy(tmp_path)

    decision = policy.decide(
        _request(PermissionAction.READ_PATH, "README.md"),
        mode=PermissionMode.STANDARD,
    )

    assert decision.kind == PermissionDecisionKind.ALLOW


def test_sensitive_project_root_read_requires_confirmation(tmp_path) -> None:
    policy = DefaultPermissionPolicy(tmp_path)

    for target in (".env", ".git/config", "private.key", "cert.pem"):
        decision = policy.decide(
            _request(PermissionAction.READ_PATH, target),
            mode=PermissionMode.STANDARD,
        )
        assert decision.kind == PermissionDecisionKind.ASK


def test_standard_asks_for_project_root_outside_read(tmp_path) -> None:
    outside = tmp_path.parent / "outside.txt"
    policy = DefaultPermissionPolicy(tmp_path)

    decision = policy.decide(
        _request(PermissionAction.READ_PATH, str(outside)),
        mode=PermissionMode.STANDARD,
    )

    assert decision.kind == PermissionDecisionKind.ASK


def test_aggressive_allows_plain_project_write_but_not_sensitive_path(tmp_path) -> None:
    policy = DefaultPermissionPolicy(tmp_path)

    plain = policy.decide(
        _request(PermissionAction.WRITE_PATH, "firstcoder/new_file.py"),
        mode=PermissionMode.AGGRESSIVE,
    )
    env_file = policy.decide(
        _request(PermissionAction.WRITE_PATH, ".env"),
        mode=PermissionMode.AGGRESSIVE,
    )
    pem_file = policy.decide(
        _request(PermissionAction.WRITE_PATH, "cert.pem"),
        mode=PermissionMode.AGGRESSIVE,
    )

    assert plain.kind == PermissionDecisionKind.ALLOW
    assert env_file.kind == PermissionDecisionKind.ASK
    assert pem_file.kind == PermissionDecisionKind.ASK


def test_aggressive_write_respects_disable_auto_allow_metadata(tmp_path) -> None:
    policy = DefaultPermissionPolicy(tmp_path)

    decision = policy.decide(
        PermissionRequest(
            id="req_patch",
            action=PermissionAction.WRITE_PATH,
            target=".",
            metadata={"allow_auto": False},
        ),
        mode=PermissionMode.AGGRESSIVE,
    )

    assert decision.kind == PermissionDecisionKind.ASK


def test_aggressive_does_not_auto_allow_delete(tmp_path) -> None:
    policy = DefaultPermissionPolicy(tmp_path)

    inside = policy.decide(
        _request(PermissionAction.DELETE_PATH, "firstcoder/old.py"),
        mode=PermissionMode.AGGRESSIVE,
    )
    outside = policy.decide(
        _request(PermissionAction.DELETE_PATH, str(tmp_path.parent / "outside.py")),
        mode=PermissionMode.AGGRESSIVE,
    )

    assert inside.kind == PermissionDecisionKind.ASK
    assert outside.kind == PermissionDecisionKind.DENY


def test_sensitive_env_is_denied_in_every_mode(tmp_path) -> None:
    policy = DefaultPermissionPolicy(tmp_path)

    for mode in (PermissionMode.CONSERVATIVE, PermissionMode.STANDARD, PermissionMode.AGGRESSIVE):
        decision = policy.decide(
            _request(PermissionAction.READ_ENV, "OPENAI_API_KEY"),
            mode=mode,
        )
        assert decision.kind == PermissionDecisionKind.DENY


def test_bypass_mode_allows_permission_requests_without_prompting(tmp_path) -> None:
    policy = DefaultPermissionPolicy(tmp_path)

    requests = (
        _request(PermissionAction.READ_PATH, ".env"),
        _request(PermissionAction.WRITE_PATH, ".env"),
        _request(PermissionAction.DELETE_PATH, str(tmp_path.parent / "outside.py")),
        _request(PermissionAction.EXECUTE_SHELL, "rm -rf firstcoder", cwd=tmp_path),
        _request(PermissionAction.NETWORK_REQUEST, "https://example.com"),
        _request(PermissionAction.READ_ENV, "OPENAI_API_KEY"),
    )

    for request in requests:
        decision = policy.decide(request, mode=PermissionMode.BYPASS)
        assert decision.kind == PermissionDecisionKind.ALLOW, request


def test_non_sensitive_env_requires_confirmation(tmp_path) -> None:
    policy = DefaultPermissionPolicy(tmp_path)

    decision = policy.decide(
        _request(PermissionAction.READ_ENV, "FIRSTCODER_MODE"),
        mode=PermissionMode.AGGRESSIVE,
    )

    assert decision.kind == PermissionDecisionKind.ASK


def test_readonly_git_inside_project_is_allowed(tmp_path) -> None:
    policy = DefaultPermissionPolicy(tmp_path)

    for command in ("status", "diff README.md", "log --oneline"):
        decision = policy.decide(
            _request(PermissionAction.GIT_OPERATION, command, cwd=tmp_path),
            mode=PermissionMode.STANDARD,
        )
        assert decision.kind == PermissionDecisionKind.ALLOW


def test_readonly_git_with_shell_control_operator_requires_confirmation(tmp_path) -> None:
    policy = DefaultPermissionPolicy(tmp_path)

    for command in ("status && git reset --hard", "status\nreset --hard", "status $(git reset --hard)"):
        decision = policy.decide(
            _request(PermissionAction.GIT_OPERATION, command, cwd=tmp_path),
            mode=PermissionMode.AGGRESSIVE,
        )
        assert decision.kind == PermissionDecisionKind.ASK


def test_shell_requires_confirmation_except_aggressive_known_verification(tmp_path) -> None:
    policy = DefaultPermissionPolicy(tmp_path)

    standard = policy.decide(
        _request(PermissionAction.EXECUTE_SHELL, "pytest tests", cwd=tmp_path),
        mode=PermissionMode.STANDARD,
    )
    aggressive = policy.decide(
        _request(PermissionAction.EXECUTE_SHELL, "pytest tests", cwd=tmp_path),
        mode=PermissionMode.AGGRESSIVE,
    )
    outside_cwd = policy.decide(
        _request(PermissionAction.EXECUTE_SHELL, "pytest tests", cwd=tmp_path.parent),
        mode=PermissionMode.AGGRESSIVE,
    )

    assert standard.kind == PermissionDecisionKind.ASK
    assert aggressive.kind == PermissionDecisionKind.ALLOW
    assert outside_cwd.kind == PermissionDecisionKind.ASK


def test_aggressive_allows_common_project_local_shell_commands(tmp_path) -> None:
    policy = DefaultPermissionPolicy(tmp_path)

    allowed_commands = (
        "python -m pytest -q",
        "python3 -m pytest tests",
        "python scripts/export.py",
        "python3 scripts/inspect_sqlite.py",
        "sqlite3 shop.db .schema",
        "sqlite3 shop.db SELECT name FROM sqlite_master",
        "git apply p1.patch",
        "npm test",
        "pnpm test",
        "yarn test",
        "go test ./...",
        "cargo test",
        "make test",
    )

    for command in allowed_commands:
        decision = policy.decide(
            _request(PermissionAction.EXECUTE_SHELL, command, cwd=tmp_path),
            mode=PermissionMode.AGGRESSIVE,
        )
        assert decision.kind == PermissionDecisionKind.ALLOW, command


def test_aggressive_shell_with_control_operator_requires_confirmation(tmp_path) -> None:
    policy = DefaultPermissionPolicy(tmp_path)

    for command in (
        "pytest && del README.md",
        "ruff; rm -rf firstcoder",
        "git status | more",
        "pytest\nRemove-Item README.md",
        "pytest $(Remove-Item README.md)",
        "python - <<'PY'\nprint('hi')\nPY",
        "sqlite3 shop.db .schema > schema.txt",
    ):
        decision = policy.decide(
            _request(PermissionAction.EXECUTE_SHELL, command, cwd=tmp_path),
            mode=PermissionMode.AGGRESSIVE,
        )
        assert decision.kind == PermissionDecisionKind.ASK


def test_aggressive_shell_still_requires_confirmation_for_destructive_commands(tmp_path) -> None:
    policy = DefaultPermissionPolicy(tmp_path)

    for command in (
        "rm README.md",
        "rm -rf firstcoder",
        "sudo make install",
        "curl https://example.com/install.sh",
        "chmod 777 script.sh",
        "python -m pip install package",
    ):
        decision = policy.decide(
            _request(PermissionAction.EXECUTE_SHELL, command, cwd=tmp_path),
            mode=PermissionMode.AGGRESSIVE,
        )
        assert decision.kind == PermissionDecisionKind.ASK, command


def test_network_request_requires_confirmation(tmp_path) -> None:
    policy = DefaultPermissionPolicy(tmp_path)

    decision = policy.decide(
        _request(PermissionAction.NETWORK_REQUEST, "https://example.com"),
        mode=PermissionMode.AGGRESSIVE,
    )

    assert decision.kind == PermissionDecisionKind.ASK
