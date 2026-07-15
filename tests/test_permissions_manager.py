from firstcoder.permissions.grants import PermissionGrantStore
from firstcoder.permissions.manager import PermissionManager
from firstcoder.permissions.policy import DefaultPermissionPolicy
from firstcoder.permissions.types import (
    PermissionAction,
    PermissionConfirmationChoice,
    PermissionDecisionKind,
    PermissionGrant,
    PermissionMode,
    PermissionPersistence,
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


def test_manager_bypass_allows_without_default_prompts(tmp_path) -> None:
    manager = PermissionManager(
        policy=DefaultPermissionPolicy(tmp_path),
        mode=PermissionMode.BYPASS,
    )

    decision = manager.preflight(
        PermissionRequest(id="req_shell", action=PermissionAction.EXECUTE_SHELL, target="rm README.md")
    )

    assert decision.kind == PermissionDecisionKind.ALLOW


def test_manager_deny_grant_still_overrides_bypass_mode(tmp_path) -> None:
    manager = PermissionManager(
        policy=DefaultPermissionPolicy(tmp_path),
        grants=PermissionGrantStore(
            [
                PermissionGrant(
                    id="deny_shell",
                    effect="deny",
                    action=PermissionAction.EXECUTE_SHELL,
                    scope_type=PermissionScopeType.COMMAND_PREFIX,
                    scope_value="rm README.md",
                    created_at="2026-07-03T00:00:00+00:00",
                )
            ]
        ),
        mode=PermissionMode.BYPASS,
    )

    decision = manager.preflight(
        PermissionRequest(id="req_shell", action=PermissionAction.EXECUTE_SHELL, target="rm README.md")
    )

    assert decision.kind == PermissionDecisionKind.DENY
    assert decision.grant is not None
    assert decision.grant.id == "deny_shell"


def test_manager_builds_permission_confirmation_request(tmp_path) -> None:
    manager = PermissionManager(policy=DefaultPermissionPolicy(tmp_path))
    request = PermissionRequest(
        id="perm_write",
        action=PermissionAction.WRITE_PATH,
        target=str(tmp_path / "firstcoder" / "new.py"),
        reason="需要写入实现文件",
    )

    confirmation = manager.build_confirmation(request)

    assert confirmation.id == "perm_write"
    assert confirmation.kind == "permission_confirmation"
    assert "write_path" in confirmation.question
    assert [option.id for option in confirmation.options] == [
        PermissionConfirmationChoice.DENY.value,
        PermissionConfirmationChoice.ALLOW_ONCE.value,
        PermissionConfirmationChoice.ALLOW_ALWAYS_SAME_SCOPE.value,
    ]
    assert confirmation.payload["permission_request_id"] == "perm_write"
    assert confirmation.payload["scope_type"] == PermissionScopeType.EXACT_PATH.value


def test_manager_path_confirmation_scope_uses_project_root_when_cwd_missing(tmp_path) -> None:
    manager = PermissionManager(policy=DefaultPermissionPolicy(tmp_path))
    request = PermissionRequest(
        id="perm_write",
        action=PermissionAction.WRITE_PATH,
        target="README.md",
    )

    confirmation = manager.build_confirmation(request)
    decision = manager.resolve_confirmation(request, "allow_always_same_scope")

    assert confirmation.payload["scope_value"] == str((tmp_path / "README.md").resolve())
    assert decision.grant is not None
    assert decision.grant.scope_value == str((tmp_path / "README.md").resolve())


def test_manager_path_allow_always_matches_later_cwd_missing_request(tmp_path) -> None:
    manager = PermissionManager(policy=DefaultPermissionPolicy(tmp_path))
    request = PermissionRequest(
        id="perm_write",
        action=PermissionAction.WRITE_PATH,
        target="README.md",
    )

    manager.resolve_confirmation(request, "allow_always_same_scope")
    later = manager.preflight(
        PermissionRequest(
            id="perm_write_later",
            action=PermissionAction.WRITE_PATH,
            target="README.md",
        )
    )

    assert later.kind == PermissionDecisionKind.ALLOW
    assert later.grant is not None
    assert later.grant.scope_value == str((tmp_path / "README.md").resolve())


def test_manager_resolves_deny_and_allow_once_without_grant(tmp_path) -> None:
    manager = PermissionManager(policy=DefaultPermissionPolicy(tmp_path))
    request = PermissionRequest(id="perm_shell", action=PermissionAction.EXECUTE_SHELL, target="pytest tests")

    denied = manager.resolve_confirmation(request, "deny")
    allowed_once = manager.resolve_confirmation(request, "2")

    assert denied.kind == PermissionDecisionKind.DENY
    assert allowed_once.kind == PermissionDecisionKind.ALLOW
    assert allowed_once.persistence == PermissionPersistence.ONCE
    assert manager.grants.list() == []


def test_manager_resolves_allow_always_and_adds_same_scope_grant(tmp_path) -> None:
    manager = PermissionManager(policy=DefaultPermissionPolicy(tmp_path))
    request = PermissionRequest(id="perm_shell", action=PermissionAction.EXECUTE_SHELL, target="pytest tests")

    decision = manager.resolve_confirmation(request, "allow_always_same_scope")

    assert decision.kind == PermissionDecisionKind.ALLOW
    assert decision.persistence == PermissionPersistence.ALWAYS
    assert decision.grant is not None
    assert decision.grant.action == PermissionAction.EXECUTE_SHELL
    assert decision.grant.scope_type == PermissionScopeType.COMMAND_PREFIX
    assert decision.grant.scope_value == "pytest tests"
    assert manager.grants.list() == [decision.grant]


def test_manager_omits_allow_always_when_request_disables_persistence(tmp_path) -> None:
    manager = PermissionManager(policy=DefaultPermissionPolicy(tmp_path))
    request = PermissionRequest(
        id="perm_python",
        action=PermissionAction.EXECUTE_SHELL,
        target="python -c",
        metadata={"allow_always": False},
    )

    confirmation = manager.build_confirmation(request)
    decision = manager.resolve_confirmation(request, "allow_always_same_scope")

    assert [option.id for option in confirmation.options] == [
        PermissionConfirmationChoice.DENY.value,
        PermissionConfirmationChoice.ALLOW_ONCE.value,
    ]
    assert confirmation.payload["allow_always"] is False
    assert decision.kind == PermissionDecisionKind.DENY
    assert decision.grant is None
    assert manager.grants.list() == []


def test_manager_shell_allow_always_does_not_expand_interpreter_scope(tmp_path) -> None:
    manager = PermissionManager(policy=DefaultPermissionPolicy(tmp_path))
    request = PermissionRequest(
        id="perm_python",
        action=PermissionAction.EXECUTE_SHELL,
        target="python -m pytest tests",
    )

    decision = manager.resolve_confirmation(request, "allow_always_same_scope")

    assert decision.grant is not None
    assert decision.grant.scope_value == "python -m pytest tests"
    miss = manager.preflight(
        PermissionRequest(id="perm_other_python", action=PermissionAction.EXECUTE_SHELL, target="python setup.py build")
    )
    assert miss.kind == PermissionDecisionKind.ASK


def test_manager_allow_always_host_scope_is_canonical(tmp_path) -> None:
    manager = PermissionManager(policy=DefaultPermissionPolicy(tmp_path))
    request = PermissionRequest(
        id="perm_net",
        action=PermissionAction.NETWORK_REQUEST,
        target="https://Example.com:443/path",
    )

    decision = manager.resolve_confirmation(request, "3")

    assert decision.grant is not None
    assert decision.grant.scope_type == PermissionScopeType.HOST
    assert decision.grant.scope_value == "example.com"


def test_manager_does_not_create_grant_for_policy_denied_request(tmp_path) -> None:
    manager = PermissionManager(policy=DefaultPermissionPolicy(tmp_path))
    request = PermissionRequest(id="perm_env", action=PermissionAction.READ_ENV, target="OPENAI_API_KEY")

    decision = manager.resolve_confirmation(request, "allow_always_same_scope")

    assert decision.kind == PermissionDecisionKind.DENY
    assert decision.grant is None
    assert manager.grants.list() == []


def test_manager_unknown_choice_does_not_create_grant(tmp_path) -> None:
    manager = PermissionManager(policy=DefaultPermissionPolicy(tmp_path))
    request = PermissionRequest(id="perm_shell", action=PermissionAction.EXECUTE_SHELL, target="pytest tests")

    decision = manager.resolve_confirmation(request, "please always allow")

    assert decision.kind == PermissionDecisionKind.DENY
    assert decision.grant is None
    assert manager.grants.list() == []


def test_manager_mcp_allow_always_is_exact_server_and_tool_scope(tmp_path) -> None:
    manager = PermissionManager(policy=DefaultPermissionPolicy(tmp_path))
    request = PermissionRequest(
        id="perm_lark_calendar_list",
        action=PermissionAction.MCP_TOOL,
        target="lark/calendar_list",
    )

    decision = manager.resolve_confirmation(request, "allow_always_same_scope")

    assert decision.grant is not None
    assert decision.grant.scope_type == PermissionScopeType.MCP_TOOL
    assert decision.grant.scope_value == "lark/calendar_list"
    assert manager.preflight(
        PermissionRequest(
            id="perm_lark_calendar_create",
            action=PermissionAction.MCP_TOOL,
            target="lark/calendar_create",
        )
    ).kind == PermissionDecisionKind.ASK
