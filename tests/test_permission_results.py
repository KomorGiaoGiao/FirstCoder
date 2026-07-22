from firstcoder.runtime.user_input import UserInputOption, UserInputRequest, user_input_request_from_tool_result
from firstcoder.permissions.types import PermissionAction, PermissionDecision, PermissionDecisionKind, PermissionRequest
from firstcoder.providers.types import ToolCall
from firstcoder.tools.permission_results import make_permission_confirmation_result, make_permission_denied_result


def test_permission_confirmation_result_round_trips_to_user_input_request() -> None:
    request = PermissionRequest(
        id="perm_1",
        action=PermissionAction.EXECUTE_SHELL,
        target="pytest tests",
        reason="运行测试",
    )
    confirmation = UserInputRequest(
        id="perm_1",
        kind="permission_confirmation",
        question="允许执行 pytest 吗？",
        options=[
            UserInputOption(id="deny", label="Deny"),
            UserInputOption(id="allow_once", label="Allow once"),
        ],
    )
    tool_call = ToolCall(id="call_shell", name="shell", arguments={"command": "pytest tests"})

    result = make_permission_confirmation_result(
        tool_name="shell",
        request=request,
        confirmation=confirmation,
        pending_tool_call=tool_call,
    )
    pending = user_input_request_from_tool_result(result, tool_call_id="call_perm", tool_name="shell")

    assert result.ok is True
    assert result.data["requires_user_input"] is True
    assert result.data["request_type"] == "permission_confirmation"
    assert result.data["permission_request_id"] == "perm_1"
    assert result.data["pending_tool_call"] == {
        "id": "call_shell",
        "name": "shell",
        "arguments": {"command": "pytest tests"},
    }
    assert pending is not None
    assert pending.kind == "permission_confirmation"
    assert pending.id == "perm_1"
    assert [option.id for option in pending.options] == ["deny", "allow_once"]
    assert pending.payload["permission_request"] == result.data["permission_request"]
    assert pending.payload["pending_tool_call"] == result.data["pending_tool_call"]


def test_permission_denied_result_has_structured_failure_data() -> None:
    request = PermissionRequest(id="perm_1", action=PermissionAction.WRITE_PATH, target="README.md")
    decision = PermissionDecision(kind=PermissionDecisionKind.DENY, reason="用户拒绝了权限请求。")

    result = make_permission_denied_result(tool_name="write", request=request, decision=decision)

    assert result.ok is False
    assert result.error == "用户拒绝了权限请求。"
    assert result.data["request_type"] == "permission_denied"
    assert result.data["permission_request_id"] == "perm_1"
    assert result.data["permission_decision"] == "deny"
    assert result.data["permission_request"]["action"] == "write_path"
