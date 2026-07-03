"""ExecutionSandbox tests."""

from __future__ import annotations

from pathlib import Path

from firstcoder.utils.execution_sandbox import ExecutionSandbox
from firstcoder.utils.sandbox_access import SandboxAccess, SandboxAccessMode


def test_execution_sandbox_filters_sensitive_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("SESSION_TOKEN", "token")
    monkeypatch.setenv("NORMAL_FLAG", "keep")

    sandbox = ExecutionSandbox(tmp_path)

    env = sandbox.build_env()

    assert env["PATH"] == "/usr/bin"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["NORMAL_FLAG"] == "keep"
    assert "OPENAI_API_KEY" not in env
    assert "SESSION_TOKEN" not in env


def test_execution_sandbox_extra_env_cannot_reintroduce_sensitive_keys(tmp_path):
    sandbox = ExecutionSandbox(tmp_path)

    env = sandbox.build_env(extra_env={"CUSTOM": "1", "API_TOKEN": "secret"})

    assert env["CUSTOM"] == "1"
    assert "API_TOKEN" not in env


def test_execution_sandbox_resolves_cwd_inside_root(tmp_path):
    (tmp_path / "pkg").mkdir()
    sandbox = ExecutionSandbox(tmp_path)

    assert sandbox.resolve_cwd("pkg") == tmp_path / "pkg"


def test_execution_sandbox_rejects_cwd_outside_root(tmp_path):
    sandbox = ExecutionSandbox(tmp_path)

    result = sandbox.run(["echo", "hi"], cwd=Path(".."))

    assert result.ok is False
    assert "超出项目目录" in result.error


def test_execution_sandbox_unrestricted_allows_cwd_outside_root(tmp_path):
    access = SandboxAccess(SandboxAccessMode.UNRESTRICTED)
    sandbox = ExecutionSandbox(tmp_path, access=access)

    assert sandbox.resolve_cwd(Path("..")) == tmp_path.parent.resolve()
