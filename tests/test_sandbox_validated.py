"""PathSandbox.resolve_validated 测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from firstcoder.utils.sandbox import PathSandbox


def test_resolve_validated_file_exists(tmp_path):
    target = tmp_path / "hello.txt"
    target.write_text("hi", encoding="utf-8")
    sandbox = PathSandbox(tmp_path)

    resolved = sandbox.resolve_validated("hello.txt")

    assert resolved == target


def test_resolve_validated_dir_exists(tmp_path):
    (tmp_path / "subdir").mkdir()
    sandbox = PathSandbox(tmp_path)

    resolved = sandbox.resolve_validated("subdir")

    assert resolved.is_dir()


def test_resolve_validated_file_expect_file(tmp_path):
    target = tmp_path / "hello.txt"
    target.write_text("hi", encoding="utf-8")
    sandbox = PathSandbox(tmp_path)

    resolved = sandbox.resolve_validated("hello.txt", expect="file")

    assert resolved == target


def test_resolve_validated_dir_expect_dir(tmp_path):
    (tmp_path / "subdir").mkdir()
    sandbox = PathSandbox(tmp_path)

    resolved = sandbox.resolve_validated("subdir", expect="dir")

    assert resolved.is_dir()


def test_resolve_validated_path_not_found(tmp_path):
    sandbox = PathSandbox(tmp_path)

    with pytest.raises(ValueError, match="路径不存在"):
        sandbox.resolve_validated("missing.txt")


def test_resolve_validated_path_not_a_file(tmp_path):
    (tmp_path / "subdir").mkdir()
    sandbox = PathSandbox(tmp_path)

    with pytest.raises(ValueError, match="路径不是文件"):
        sandbox.resolve_validated("subdir", expect="file")


def test_resolve_validated_path_not_a_dir(tmp_path):
    target = tmp_path / "hello.txt"
    target.write_text("hi", encoding="utf-8")
    sandbox = PathSandbox(tmp_path)

    with pytest.raises(ValueError, match="路径不是目录"):
        sandbox.resolve_validated("hello.txt", expect="dir")


def test_resolve_validated_rejects_escape(tmp_path):
    sandbox = PathSandbox(tmp_path)

    with pytest.raises(ValueError, match="路径超出项目目录"):
        sandbox.resolve_validated("../outside.txt")
