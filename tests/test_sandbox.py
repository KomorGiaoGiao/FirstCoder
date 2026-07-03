"""路径沙箱测试。"""

from __future__ import annotations

import pytest

from firstcoder.utils.sandbox import PathSandbox
from firstcoder.utils.sandbox_access import SandboxAccess, SandboxAccessMode


def test_path_sandbox_resolves_relative_path_inside_root(tmp_path):
    sandbox = PathSandbox(tmp_path)

    resolved = sandbox.resolve("src/main.py")

    assert resolved == tmp_path.resolve() / "src" / "main.py"


def test_path_sandbox_rejects_parent_escape(tmp_path):
    sandbox = PathSandbox(tmp_path)

    with pytest.raises(ValueError, match="路径超出项目目录"):
        sandbox.resolve("../outside.txt")


def test_path_sandbox_unrestricted_allows_parent_escape(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    access = SandboxAccess(SandboxAccessMode.UNRESTRICTED)
    sandbox = PathSandbox(tmp_path, access=access)

    assert sandbox.resolve("../outside.txt") == outside.resolve()


def test_path_sandbox_returns_posix_relative_path(tmp_path):
    target = tmp_path / "dir" / "file.txt"
    target.parent.mkdir()
    target.write_text("hello", encoding="utf-8")
    sandbox = PathSandbox(tmp_path)

    assert sandbox.relative(target) == "dir/file.txt"


def test_path_sandbox_unrestricted_relative_formats_external_path(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("hello", encoding="utf-8")
    access = SandboxAccess(SandboxAccessMode.UNRESTRICTED)
    sandbox = PathSandbox(tmp_path, access=access)

    assert sandbox.relative(outside) == str(outside.resolve())
