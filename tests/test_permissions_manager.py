from firstcoder.permissions.grants import PermissionGrantStore
from firstcoder.permissions.manager import PermissionManager
from firstcoder.permissions.policy import DefaultPermissionPolicy
from firstcoder.permissions.types import (
    PermissionAction,
    PermissionDecisionKind,
    PermissionGrant,
    PermissionMode,
    PermissionRequest,
    PermissionScopeType,
)


def test_manager_uses_matching_grant_before_default_policy(tmp_path) -> None:
    manager = PermissionManager(
        policy=DefaultPermissionPolicy(tmp_path),
        grants=PermissionGrantStore(
            [
                PermissionGrant(
                    id="grant_shell",
                    effect="allow",
                    action=PermissionAction.EXECUTE_SHELL,
                    scope_type=PermissionScopeType.COMMAND_PREFIX,
                    scope_value="npm test",
                    created_at="2026-06-04T00:00:00+08:00",
                )
            ]
        ),
        mode=PermissionMode.STANDARD,
    )

    decision = manager.preflight(
        PermissionRequest(id="req_1", action=PermissionAction.EXECUTE_SHELL, target="npm test -- --watch=false")
    )

    assert decision.kind == PermissionDecisionKind.ALLOW
    assert decision.grant is not None
    assert decision.grant.id == "grant_shell"


def test_manager_falls_back_to_mode_aware_policy(tmp_path) -> None:
    manager = PermissionManager(
        policy=DefaultPermissionPolicy(tmp_path),
        mode=PermissionMode.AGGRESSIVE,
    )

    decision = manager.preflight(
        PermissionRequest(id="req_1", action=PermissionAction.WRITE_PATH, target="firstcoder/new.py")
    )

    assert decision.kind == PermissionDecisionKind.ALLOW


def test_manager_deny_grant_still_overrides_aggressive_policy(tmp_path) -> None:
    manager = PermissionManager(
        policy=DefaultPermissionPolicy(tmp_path),
        grants=PermissionGrantStore(
            [
                PermissionGrant(
                    id="deny_write_tree",
                    effect="deny",
                    action=PermissionAction.WRITE_PATH,
                    scope_type=PermissionScopeType.PATH_TREE,
                    scope_value=str(tmp_path / "firstcoder"),
                    created_at="2026-06-04T00:00:00+08:00",
                )
            ]
        ),
        mode=PermissionMode.AGGRESSIVE,
    )

    decision = manager.preflight(
        PermissionRequest(
            id="req_1",
            action=PermissionAction.WRITE_PATH,
            target=str(tmp_path / "firstcoder" / "new.py"),
        )
    )

    assert decision.kind == PermissionDecisionKind.DENY
    assert decision.grant is not None
    assert decision.grant.id == "deny_write_tree"
