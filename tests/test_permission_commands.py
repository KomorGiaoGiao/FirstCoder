from firstcoder.agent.session import AgentSession
from firstcoder.app.permission_commands import PermissionCommandHandler
from firstcoder.context.store import JsonlSessionStore
from firstcoder.permissions.types import PermissionMode
from firstcoder.tools.builtin import create_builtin_registry
from firstcoder.utils.sandbox_access import SandboxAccess, SandboxAccessMode


def test_permission_mode_command_shows_current_mode(tmp_path) -> None:
    session = AgentSession.from_project(
        store=JsonlSessionStore(tmp_path / ".firstcoder"),
        session_id="sess_mode",
        project_root=tmp_path,
        tools=[],
    )
    handler = PermissionCommandHandler(session=session)

    result = handler.handle("/mode")

    assert result.handled is True
    assert "Permission mode: standard" in result.output


def test_permission_mode_command_updates_session_and_manager(tmp_path) -> None:
    session = AgentSession.from_project(
        store=JsonlSessionStore(tmp_path / ".firstcoder"),
        session_id="sess_mode",
        project_root=tmp_path,
        tools=[],
    )
    handler = PermissionCommandHandler(session=session)

    result = handler.handle("/mode aggressive")

    assert result.handled is True
    assert result.output == "Permission mode set to: aggressive"
    assert session.mode == PermissionMode.AGGRESSIVE.value
    assert session.permission_manager is not None
    assert session.permission_manager.mode == PermissionMode.AGGRESSIVE


def test_permission_mode_command_accepts_bypass(tmp_path) -> None:
    access = SandboxAccess()
    session = AgentSession.from_project(
        store=JsonlSessionStore(tmp_path / ".firstcoder"),
        session_id="sess_mode",
        project_root=tmp_path,
        tools=create_builtin_registry(tmp_path, access=access).tools(),
        sandbox_access=access,
    )
    handler = PermissionCommandHandler(session=session)

    result = handler.handle("/mode bypass")

    assert result.handled is True
    assert result.output == "Permission mode set to: bypass"
    assert session.mode == PermissionMode.BYPASS.value
    assert session.permission_manager is not None
    assert session.permission_manager.mode == PermissionMode.BYPASS
    assert access.mode == SandboxAccessMode.UNRESTRICTED
    assert session.permission_policy["path_access"] == "unrestricted"


def test_permission_mode_command_restores_project_sandbox_after_bypass(tmp_path) -> None:
    access = SandboxAccess(SandboxAccessMode.UNRESTRICTED)
    session = AgentSession.from_project(
        store=JsonlSessionStore(tmp_path / ".firstcoder"),
        session_id="sess_mode",
        project_root=tmp_path,
        tools=create_builtin_registry(tmp_path, access=access).tools(),
        sandbox_access=access,
    )
    session.set_permission_mode(PermissionMode.BYPASS)
    handler = PermissionCommandHandler(session=session)

    result = handler.handle("/mode standard")

    assert result.handled is True
    assert session.mode == PermissionMode.STANDARD.value
    assert access.mode == SandboxAccessMode.PROJECT
    assert session.permission_policy["path_access"] == "project_root_only"


def test_bypass_mode_lets_existing_tools_access_outside_project(tmp_path) -> None:
    access = SandboxAccess()
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    session = AgentSession.from_project(
        store=JsonlSessionStore(tmp_path / ".firstcoder"),
        session_id="sess_mode",
        project_root=tmp_path,
        tools=create_builtin_registry(tmp_path, access=access).tools(),
        sandbox_access=access,
    )

    denied = session.tool_registry.execute("view", {"path": str(outside)})
    session.set_permission_mode(PermissionMode.BYPASS)
    allowed = session.tool_registry.execute("view", {"path": str(outside)})
    session.set_permission_mode(PermissionMode.STANDARD)
    denied_again = session.tool_registry.execute("view", {"path": str(outside)})

    assert denied.ok is False
    assert allowed.ok is True
    assert "secret" in allowed.content
    assert denied_again.ok is False


def test_permission_mode_command_rejects_unknown_mode(tmp_path) -> None:
    session = AgentSession.from_project(
        store=JsonlSessionStore(tmp_path / ".firstcoder"),
        session_id="sess_mode",
        project_root=tmp_path,
        tools=[],
    )
    handler = PermissionCommandHandler(session=session)

    result = handler.handle("/mode chaos")

    assert result.handled is True
    assert "Unknown permission mode" in result.output
    assert session.mode == PermissionMode.STANDARD.value
