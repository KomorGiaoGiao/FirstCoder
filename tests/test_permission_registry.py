from firstcoder.permissions.grants import PermissionGrantStore
from firstcoder.permissions.manager import PermissionManager
from firstcoder.permissions.policy import DefaultPermissionPolicy
from firstcoder.permissions.types import (
    PermissionAction,
    PermissionGrant,
    PermissionScopeType,
)
from firstcoder.providers.types import ToolDefinition
from firstcoder.tools.permission_registry import PermissionAwareToolRegistry, permission_request_for_tool
from firstcoder.tools.registry import ToolRegistry
from firstcoder.tools.types import Tool, ToolPermissionSpec, make_text_result


def _write_tool(calls: list[dict[str, object]] | None = None) -> Tool:
    def executor(path: str, content: str = ""):
        if calls is not None:
            calls.append({"path": path, "content": content})
        return make_text_result("write", f"wrote {path}")

    return Tool(
        definition=ToolDefinition(
            name="write",
            description="写文件。",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path"],
            },
        ),
        executor=executor,
        permission=ToolPermissionSpec(
            action=PermissionAction.WRITE_PATH,
            target_arg="path",
            reason="写入文件需要确认。",
        ),
    )


def _plain_tool(calls: list[str]) -> Tool:
    def executor(text: str):
        calls.append(text)
        return make_text_result("echo", text)

    return Tool(
        definition=ToolDefinition(
            name="echo",
            description="回显。",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        executor=executor,
    )


def test_permission_aware_registry_keeps_plain_tools_unchanged(tmp_path) -> None:
    calls: list[str] = []
    registry = PermissionAwareToolRegistry(
        ToolRegistry([_plain_tool(calls)]),
        PermissionManager(policy=DefaultPermissionPolicy(tmp_path)),
    )

    result = registry.execute("echo", {"text": "hi"})

    assert result.ok is True
    assert result.content == "hi"
    assert calls == ["hi"]


def test_permission_aware_registry_returns_confirmation_without_executing(tmp_path) -> None:
    calls: list[dict[str, object]] = []
    registry = PermissionAwareToolRegistry(
        ToolRegistry([_write_tool(calls)]),
        PermissionManager(policy=DefaultPermissionPolicy(tmp_path)),
    )

    result = registry.execute("write", {"path": "README.md", "content": "hello"})

    assert result.ok is True
    assert result.data["requires_user_input"] is True
    assert result.data["request_type"] == "permission_confirmation"
    assert result.data["permission_request"]["action"] == "write_path"
    assert result.data["permission_request"]["target"] == "README.md"
    assert result.data["permission_request"]["cwd"] == str(tmp_path.resolve())
    assert calls == []


def test_permission_aware_registry_returns_denied_result(tmp_path) -> None:
    calls: list[str] = []
    registry = PermissionAwareToolRegistry(
        ToolRegistry(
            [
                Tool(
                    definition=ToolDefinition(name="env", description="读 env。", parameters={}),
                    executor=lambda key: calls.append(key) or make_text_result("env", key),
                    permission=ToolPermissionSpec(action=PermissionAction.READ_ENV, target_arg="key"),
                )
            ]
        ),
        PermissionManager(policy=DefaultPermissionPolicy(tmp_path)),
    )

    result = registry.execute("env", {"key": "OPENAI_API_KEY"})

    assert result.ok is False
    assert result.data["request_type"] == "permission_denied"
    assert result.data["permission_request"]["action"] == "read_env"
    assert calls == []


def test_permission_aware_registry_executes_after_matching_grant(tmp_path) -> None:
    calls: list[dict[str, object]] = []
    grant = PermissionGrant(
        id="grant_write_readme",
        effect="allow",
        action=PermissionAction.WRITE_PATH,
        scope_type=PermissionScopeType.EXACT_PATH,
        scope_value=str((tmp_path / "README.md").resolve()),
        created_at="2026-06-04T00:00:00+08:00",
    )
    registry = PermissionAwareToolRegistry(
        ToolRegistry([_write_tool(calls)]),
        PermissionManager(policy=DefaultPermissionPolicy(tmp_path), grants=PermissionGrantStore([grant])),
    )

    result = registry.execute("write", {"path": "README.md", "content": "hello"})

    assert result.ok is True
    assert result.content == "wrote README.md"
    assert calls == [{"path": "README.md", "content": "hello"}]


def test_permission_request_for_tool_reports_missing_target_argument() -> None:
    tool = _write_tool()

    try:
        permission_request_for_tool(tool, {"content": "hello"})
    except ValueError as exc:
        assert "path" in str(exc)
    else:
        raise AssertionError("expected missing target argument error")


def test_permission_aware_registry_missing_target_argument_does_not_execute(tmp_path) -> None:
    calls: list[dict[str, object]] = []
    registry = PermissionAwareToolRegistry(
        ToolRegistry([_write_tool(calls)]),
        PermissionManager(policy=DefaultPermissionPolicy(tmp_path)),
    )

    result = registry.execute("write", {"content": "hello"})

    assert result.ok is False
    assert "path" in (result.error or "")
    assert calls == []


def test_permission_request_id_is_stable_for_argument_order() -> None:
    tool = _write_tool()

    first = permission_request_for_tool(tool, {"path": "README.md", "content": "hello"})
    second = permission_request_for_tool(tool, {"content": "hello", "path": "README.md"})

    assert first.id == second.id


def test_permission_aware_registry_normalizes_relative_cwd_arg(tmp_path) -> None:
    (tmp_path / "pkg").mkdir()
    calls: list[dict[str, object]] = []
    tool = Tool(
        definition=ToolDefinition(
            name="shell",
            description="运行命令。",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}},
                "required": ["command"],
            },
        ),
        executor=lambda command, cwd=".": calls.append({"command": command, "cwd": cwd}) or make_text_result("shell", "ok"),
        permission=ToolPermissionSpec(
            action=PermissionAction.EXECUTE_SHELL,
            target_arg="command",
            cwd_arg="cwd",
        ),
    )
    registry = PermissionAwareToolRegistry(
        ToolRegistry([tool]),
        PermissionManager(policy=DefaultPermissionPolicy(tmp_path)),
    )

    result = registry.execute("shell", {"command": "pytest", "cwd": "pkg"})

    assert result.ok is True
    assert result.data["requires_user_input"] is True
    assert result.data["permission_request"]["cwd"] == str((tmp_path / "pkg").resolve())
    assert calls == []
