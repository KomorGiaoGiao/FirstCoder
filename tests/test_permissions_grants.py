from firstcoder.permissions.grants import PermissionGrantStore
from firstcoder.permissions.types import (
    PermissionAction,
    PermissionDecisionKind,
    PermissionGrant,
    PermissionPersistence,
    PermissionRequest,
    PermissionScopeType,
)


def _grant(
    grant_id: str,
    *,
    effect: str = "allow",
    action: PermissionAction = PermissionAction.EXECUTE_SHELL,
    scope_type: PermissionScopeType = PermissionScopeType.COMMAND_PREFIX,
    scope_value: str = "pytest",
) -> PermissionGrant:
    return PermissionGrant(
        id=grant_id,
        effect=effect,  # type: ignore[arg-type]
        action=action,
        scope_type=scope_type,
        scope_value=scope_value,
        created_at="2026-06-04T00:00:00+08:00",
    )


def test_command_prefix_grant_matches_same_prefix() -> None:
    store = PermissionGrantStore([_grant("grant_pytest")])
    request = PermissionRequest(
        id="req_1",
        action=PermissionAction.EXECUTE_SHELL,
        target="pytest tests/test_permissions_policy.py",
    )

    decision = store.matching_decision(request)

    assert decision is not None
    assert decision.kind == PermissionDecisionKind.ALLOW
    assert decision.persistence == PermissionPersistence.ALWAYS
    assert decision.grant is not None
    assert decision.grant.id == "grant_pytest"


def test_command_prefix_grant_does_not_match_partial_word() -> None:
    store = PermissionGrantStore([_grant("grant_pytest")])
    request = PermissionRequest(
        id="req_1",
        action=PermissionAction.EXECUTE_SHELL,
        target="pytest-watch tests",
    )

    assert store.matching_decision(request) is None


def test_shell_command_prefix_grant_does_not_match_compound_command() -> None:
    store = PermissionGrantStore([_grant("grant_pytest")])

    for command in (
        "pytest && del README.md",
        "pytest\nRemove-Item README.md",
        "pytest $(Remove-Item README.md)",
    ):
        request = PermissionRequest(
            id="req_1",
            action=PermissionAction.EXECUTE_SHELL,
            target=command,
        )
        assert store.matching_decision(request) is None


def test_path_grants_match_exact_path_and_tree(tmp_path) -> None:
    exact = tmp_path / "README.md"
    tree = tmp_path / "firstcoder"
    store = PermissionGrantStore(
        [
            _grant(
                "grant_exact",
                action=PermissionAction.READ_PATH,
                scope_type=PermissionScopeType.EXACT_PATH,
                scope_value=str(exact),
            ),
            _grant(
                "grant_tree",
                action=PermissionAction.WRITE_PATH,
                scope_type=PermissionScopeType.PATH_TREE,
                scope_value=str(tree),
            ),
        ]
    )

    exact_decision = store.matching_decision(
        PermissionRequest(id="req_read", action=PermissionAction.READ_PATH, target=str(exact))
    )
    tree_decision = store.matching_decision(
        PermissionRequest(id="req_write", action=PermissionAction.WRITE_PATH, target=str(tree / "agent" / "loop.py"))
    )
    miss = store.matching_decision(
        PermissionRequest(id="req_miss", action=PermissionAction.WRITE_PATH, target=str(tmp_path / "tests" / "x.py"))
    )

    assert exact_decision is not None
    assert exact_decision.grant is not None
    assert exact_decision.grant.id == "grant_exact"
    assert tree_decision is not None
    assert tree_decision.grant is not None
    assert tree_decision.grant.id == "grant_tree"
    assert miss is None


def test_path_tree_grant_does_not_match_sibling_prefix(tmp_path) -> None:
    store = PermissionGrantStore(
        [
            _grant(
                "grant_tree",
                action=PermissionAction.WRITE_PATH,
                scope_type=PermissionScopeType.PATH_TREE,
                scope_value=str(tmp_path / "firstcoder"),
            )
        ]
    )

    decision = store.matching_decision(
        PermissionRequest(id="req_1", action=PermissionAction.WRITE_PATH, target=str(tmp_path / "firstcoder2" / "x.py"))
    )

    assert decision is None


def test_host_and_env_key_grants_match_normalized_values() -> None:
    store = PermissionGrantStore(
        [
            _grant(
                "grant_host",
                action=PermissionAction.NETWORK_REQUEST,
                scope_type=PermissionScopeType.HOST,
                scope_value="example.com",
            ),
            _grant(
                "grant_env",
                action=PermissionAction.READ_ENV,
                scope_type=PermissionScopeType.ENV_KEY,
                scope_value="firstcoder_mode",
            ),
        ]
    )

    host_decision = store.matching_decision(
        PermissionRequest(id="req_host", action=PermissionAction.NETWORK_REQUEST, target="https://example.com/a")
    )
    env_decision = store.matching_decision(
        PermissionRequest(id="req_env", action=PermissionAction.READ_ENV, target="FIRSTCODER_MODE")
    )

    assert host_decision is not None
    assert host_decision.grant is not None
    assert host_decision.grant.id == "grant_host"
    assert env_decision is not None
    assert env_decision.grant is not None
    assert env_decision.grant.id == "grant_env"


def test_deny_grant_wins_over_allow_grant() -> None:
    store = PermissionGrantStore(
        [
            _grant("allow_pytest", effect="allow"),
            _grant("deny_pytest", effect="deny"),
        ]
    )
    request = PermissionRequest(
        id="req_1",
        action=PermissionAction.EXECUTE_SHELL,
        target="pytest tests",
    )

    decision = store.matching_decision(request)

    assert decision is not None
    assert decision.kind == PermissionDecisionKind.DENY
    assert decision.grant is not None
    assert decision.grant.id == "deny_pytest"
