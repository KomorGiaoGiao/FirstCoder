"""写入/修改类工具行为测试。"""

from __future__ import annotations

from firstcoder.tools import create_builtin_registry
from firstcoder.tools.apply_patch import create_apply_patch_tool
from firstcoder.tools.delete import create_delete_tool
from firstcoder.tools.edit import create_edit_tool
from firstcoder.tools.write import create_write_tool


def test_apply_patch_updates_file(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("old\n", encoding="utf-8")
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    patch = """*** Begin Patch
*** Update File: app.py
@@
-old
+new
*** End Patch"""

    result = registry.execute("apply_patch", {"patch": patch})

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "new\n"
    assert result.data["changed_files"] == ["app.py"]


def test_apply_patch_dry_run_does_not_write(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("old\n", encoding="utf-8")
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    patch = """*** Begin Patch
*** Update File: app.py
@@
-old
+new
*** End Patch"""

    result = registry.execute("apply_patch", {"patch": patch, "dry_run": True})

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "old\n"
    assert result.data["dry_run"] is True


def test_apply_patch_rejects_missing_old_text(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("old\n", encoding="utf-8")
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    patch = """*** Begin Patch
*** Update File: app.py
@@
-missing
+new
*** End Patch"""

    result = registry.execute("apply_patch", {"patch": patch})

    assert result.ok is False
    assert result.error == "没有找到要替换的内容"


def test_apply_patch_adds_file(tmp_path):
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)
    patch = """*** Begin Patch
*** Add File: notes/todo.txt
+hello
+world
*** End Patch"""

    result = registry.execute("apply_patch", {"patch": patch})

    assert result.ok is True
    assert (tmp_path / "notes" / "todo.txt").read_text(encoding="utf-8") == "hello\nworld\n"
    assert result.data["created_files"] == ["notes/todo.txt"]


def test_apply_patch_deletes_file(tmp_path):
    target = tmp_path / "old.txt"
    target.write_text("delete me\n", encoding="utf-8")
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)
    patch = """*** Begin Patch
*** Delete File: old.txt
*** End Patch"""

    result = registry.execute("apply_patch", {"patch": patch})

    assert result.ok is True
    assert not target.exists()
    assert result.data["deleted_files"] == ["old.txt"]


def test_apply_patch_rejects_paths_outside_root(tmp_path):
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)
    patch = """*** Begin Patch
*** Add File: ../outside.txt
+secret
*** End Patch"""

    result = registry.execute("apply_patch", {"patch": patch})

    assert result.ok is False
    assert "超出项目目录" in result.error


def test_apply_patch_moves_file_and_updates_content(tmp_path):
    source = tmp_path / "old.py"
    source.write_text("name = 'old'\n", encoding="utf-8")
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)
    patch = """*** Begin Patch
*** Update File: old.py
*** Move to: src/new.py
@@
-name = 'old'
+name = 'new'
*** End Patch"""

    result = registry.execute("apply_patch", {"patch": patch})

    assert result.ok is True
    assert not source.exists()
    assert (tmp_path / "src" / "new.py").read_text(encoding="utf-8") == "name = 'new'\n"
    assert result.data["moved_files"] == [{"source": "old.py", "destination": "src/new.py"}]


def test_apply_patch_moves_file_without_content_changes(tmp_path):
    source = tmp_path / "old.py"
    source.write_text("print('hi')\n", encoding="utf-8")
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)
    patch = """*** Begin Patch
*** Update File: old.py
*** Move to: new.py
*** End Patch"""

    result = registry.execute("apply_patch", {"patch": patch})

    assert result.ok is True
    assert not source.exists()
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_apply_patch_dry_run_move_does_not_write(tmp_path):
    source = tmp_path / "old.py"
    source.write_text("print('hi')\n", encoding="utf-8")
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)
    patch = """*** Begin Patch
*** Update File: old.py
*** Move to: new.py
*** End Patch"""

    result = registry.execute("apply_patch", {"patch": patch, "dry_run": True})

    assert result.ok is True
    assert source.exists()
    assert not (tmp_path / "new.py").exists()


def test_apply_patch_rejects_move_destination_outside_root(tmp_path):
    (tmp_path / "old.py").write_text("print('hi')\n", encoding="utf-8")
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)
    patch = """*** Begin Patch
*** Update File: old.py
*** Move to: ../new.py
*** End Patch"""

    result = registry.execute("apply_patch", {"patch": patch})

    assert result.ok is False
    assert "超出项目目录" in result.error


def test_write_creates_utf8_file_inside_root(tmp_path):
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    result = registry.execute("write", {"path": "notes/todo.txt", "content": "你好\n"})

    assert result.ok is True
    assert (tmp_path / "notes" / "todo.txt").read_text(encoding="utf-8") == "你好\n"
    assert result.data["path"] == "notes/todo.txt"
    assert result.data["bytes_written"] > 0
    assert result.data["created"] is True


def test_write_rejects_missing_parent_when_create_dirs_is_false(tmp_path):
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    result = registry.execute(
        "write",
        {"path": "missing/todo.txt", "content": "x", "create_dirs": False},
    )

    assert result.ok is False
    assert result.error == "父目录不存在：missing"


def test_write_rejects_existing_file_when_overwrite_is_false(tmp_path):
    target = tmp_path / "todo.txt"
    target.write_text("old", encoding="utf-8")
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    result = registry.execute("write", {"path": "todo.txt", "content": "new", "overwrite": False})

    assert result.ok is False
    assert result.error == "文件已存在且 overwrite 为 False：todo.txt"
    assert target.read_text(encoding="utf-8") == "old"


def test_write_rejects_paths_outside_root(tmp_path):
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    result = registry.execute("write", {"path": "../outside.txt", "content": "secret"})

    assert result.ok is False
    assert "超出项目目录" in result.error


def test_delete_removes_file_inside_root(tmp_path):
    target = tmp_path / "todo.txt"
    target.write_text("delete me", encoding="utf-8")
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    result = registry.execute("delete", {"path": "todo.txt"})

    assert result.ok is True
    assert not target.exists()
    assert result.data["path"] == "todo.txt"
    assert result.data["type"] == "file"


def test_delete_requires_recursive_for_directory(tmp_path):
    target = tmp_path / "dir"
    target.mkdir()
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    result = registry.execute("delete", {"path": "dir"})

    assert result.ok is False
    assert result.error == "删除目录必须启用 recursive"
    assert target.exists()


def test_delete_removes_directory_when_recursive_enabled(tmp_path):
    target = tmp_path / "dir"
    target.mkdir()
    (target / "file.txt").write_text("x", encoding="utf-8")
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    result = registry.execute("delete", {"path": "dir", "recursive": True})

    assert result.ok is True
    assert not target.exists()
    assert result.data["type"] == "dir"


def test_delete_rejects_paths_outside_root(tmp_path):
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    result = registry.execute("delete", {"path": "../outside.txt"})

    assert result.ok is False
    assert "超出项目目录" in result.error


def test_delete_rejects_project_root(tmp_path):
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    result = registry.execute("delete", {"path": ".", "recursive": True})

    assert result.ok is False
    assert result.error == "不能删除项目根目录"
    assert tmp_path.exists()


import sys

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 非管理员账户创建符号链接受限，平台行为差异大")
def test_delete_treats_symlink_as_file_and_unlinks_it(tmp_path):
    """delete 工具对符号链接使用 unlink，只删除链接本身而不递归到目标。"""
    real_file = tmp_path / "real.txt"
    real_file.write_text("target content", encoding="utf-8")
    symlink = tmp_path / "link.txt"
    symlink.symlink_to(real_file)
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    result = registry.execute("delete", {"path": "link.txt"})

    assert result.ok is True
    assert not symlink.exists()
    assert real_file.exists()
    assert result.data["type"] == "file"


def test_edit_replaces_unique_text_inside_root(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("print('old')\n", encoding="utf-8")
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    result = registry.execute("edit", {"path": "app.py", "old": "old", "new": "new"})

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "print('new')\n"
    assert result.data["replacements"] == 1


def test_edit_rejects_empty_old_text(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("print('old')\n", encoding="utf-8")
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    result = registry.execute("edit", {"path": "app.py", "old": "", "new": "new"})

    assert result.ok is False
    assert result.error == "old 不能为空"


def test_edit_rejects_paths_outside_root(tmp_path):
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    result = registry.execute("edit", {"path": "../outside.txt", "old": "x", "new": "y"})

    assert result.ok is False
    assert "超出项目目录" in result.error


def test_edit_rejects_ambiguous_text_without_replace_all(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("old\nold\n", encoding="utf-8")
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    result = registry.execute("edit", {"path": "app.py", "old": "old", "new": "new"})

    assert result.ok is False
    assert result.error == "匹配内容出现 2 次；请提供更精确的 old，或启用 replace_all"


def test_edit_can_replace_all_matches_when_enabled(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("old\nold\n", encoding="utf-8")
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    result = registry.execute("edit", {"path": "app.py", "old": "old", "new": "new", "replace_all": True})

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "new\nnew\n"
    assert result.data["replacements"] == 2


def test_edit_fails_when_old_text_spans_multiple_lines(tmp_path):
    """edit 的 old 参数若跨行出现多次，由于 text.count(old) 按子串计数，仍可能匹配。"""
    target = tmp_path / "app.py"
    target.write_text("line1\nline2\nline3\nline1\nline2\nline3\n", encoding="utf-8")
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    # 跨两行的 old 文本出现了 2 次，因此默认应该拒绝
    result = registry.execute("edit", {"path": "app.py", "old": "line1\nline2", "new": "A\nB"})

    assert result.ok is False
    assert "出现 2 次" in result.error


def test_edit_can_replace_multiline_old_when_unique(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("line1\nline2\nline3\n", encoding="utf-8")
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    result = registry.execute("edit", {"path": "app.py", "old": "line1\nline2", "new": "A\nB"})

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "A\nB\nline3\n"
    assert result.data["replacements"] == 1
