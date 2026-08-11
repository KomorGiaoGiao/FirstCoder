"""utils/text 模块测试：truncate 和 safe_read_text。"""

from __future__ import annotations

from pathlib import Path

import pytest

from firstcoder.utils.text import safe_read_text, truncate, truncate_head_tail


class TestTruncate:
    def test_short_text_not_truncated(self):
        result, was_truncated = truncate("hello", 10)
        assert result == "hello"
        assert was_truncated is False

    def test_exact_length_not_truncated(self):
        result, was_truncated = truncate("hello", 5)
        assert result == "hello"
        assert was_truncated is False

    def test_long_text_truncated_with_suffix(self):
        result, was_truncated = truncate("hello world", 5)
        assert result == "hello\n\n[输出已截断]"
        assert was_truncated is True

    def test_empty_string(self):
        result, was_truncated = truncate("", 10)
        assert result == ""
        assert was_truncated is False

    def test_custom_suffix(self):
        result, was_truncated = truncate("abcdef", 3, suffix="...")
        assert result == "abc..."
        assert was_truncated is True


class TestTruncateHeadTail:
    def test_preserves_both_ends_when_truncated(self):
        result, was_truncated = truncate_head_tail("abcdefghij", 6)

        assert result.startswith("abc")
        assert result.endswith("hij")
        assert "省略 4 个字符" in result
        assert was_truncated is True

    def test_returns_original_when_within_limit(self):
        result, was_truncated = truncate_head_tail("hello", 5)

        assert result == "hello"
        assert was_truncated is False


class TestSafeReadText:
    def test_reads_utf8_file(self, tmp_path):
        target = tmp_path / "test.txt"
        target.write_text("你好世界", encoding="utf-8")

        text = safe_read_text(target)

        assert text == "你好世界"

    def test_raises_on_binary_file(self, tmp_path):
        target = tmp_path / "binary.bin"
        target.write_bytes(b"\x80\x81\x82")

        with pytest.raises(UnicodeDecodeError):
            safe_read_text(target)

    def test_raises_on_nonexistent_file(self, tmp_path):
        target = tmp_path / "missing.txt"

        with pytest.raises(FileNotFoundError):
            safe_read_text(target)
