"""`read_multi` 工具测试。"""

from __future__ import annotations

from firstcoder.tools.read_multi import create_read_multi_tool


def test_read_multi_reads_multiple_files(tmp_path):
    (tmp_path / "a.py").write_text("print('a')", encoding="utf-8")
    (tmp_path / "b.py").write_text("print('b')", encoding="utf-8")
    tool = create_read_multi_tool(tmp_path)

    result = tool.executor(paths=["a.py", "b.py"])

    assert result.ok is True
    assert result.name == "read_multi"
    assert "a.py" in result.content
    assert "print('a')" in result.content
    assert "b.py" in result.content
    assert "print('b')" in result.content
    assert len(result.data["files"]) == 2


def test_read_multi_reports_missing_files(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    tool = create_read_multi_tool(tmp_path)

    result = tool.executor(paths=["a.py", "missing.py"])

    assert result.ok is False
    assert "a.py" in result.content
    assert "missing.py" in result.content
    assert "文件不存在" in result.content


def test_read_multi_rejects_paths_outside_root(tmp_path):
    tool = create_read_multi_tool(tmp_path)

    result = tool.executor(paths=["../outside.txt"])

    assert result.ok is False
    assert "超出项目目录" in result.content


def test_read_multi_returns_error_for_all_missing(tmp_path):
    tool = create_read_multi_tool(tmp_path)

    result = tool.executor(paths=["a.py", "b.py"])

    assert result.ok is False
    assert "文件不存在" in result.content
    assert len(result.data["errors"]) == 2


def test_read_multi_respects_max_total_chars(tmp_path):
    (tmp_path / "a.py").write_text("a" * 100, encoding="utf-8")
    (tmp_path / "b.py").write_text("b" * 100, encoding="utf-8")
    tool = create_read_multi_tool(tmp_path)

    result = tool.executor(paths=["a.py", "b.py"], max_total_chars=50)

    assert result.ok is True
    assert "[已截断" in result.content
    assert result.data["truncated"] is True


def test_read_multi_definition_has_correct_schema():
    tool = create_read_multi_tool(".")

    assert tool.name == "read_multi"
    assert "paths" in tool.definition.parameters["properties"]
    assert "max_total_chars" in tool.definition.parameters["properties"]
    assert tool.definition.parameters["required"] == ["paths"]
