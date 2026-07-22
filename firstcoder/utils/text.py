"""文本处理通用工具：截断、安全读取等。"""

from __future__ import annotations

from pathlib import Path


def truncate(value: str, max_chars: int, *, suffix: str = "\n\n[输出已截断]") -> tuple[str, bool]:
    """截断文本，超出时追加后缀标记。

    多个工具都需要截断输出以控制 tool result 大小，
    统一实现避免各处重复同样的截断逻辑和后缀字符串。
    """

    if len(value) <= max_chars:
        return value, False
    return value[:max_chars] + suffix, True


def safe_read_text(path: Path, *, encoding: str = "utf-8") -> str:
    """读取文本文件，遇到编码问题直接抛出 UnicodeDecodeError。

    view、edit、read_multi、apply_patch 都有同样的 read_text + except UnicodeDecodeError 模式，
    统一到这里让调用方只需处理业务逻辑。
    """

    return path.read_text(encoding=encoding)


def optional_str(value: object) -> str | None:
    """Normalize empty-ish values to None, otherwise stringify."""

    if value in (None, ""):
        return None
    return str(value)


def display_value(value: object | None, *, empty: str = "-") -> str:
    """Render a value for UI/share text, mapping empty-ish inputs to a placeholder."""

    if value in (None, ""):
        return empty
    return str(value)


def model_label(provider: str | None, model: str | None, *, empty: str = "-") -> str:
    """Format provider/model for display."""

    if provider and model:
        return f"{provider}/{model}"
    return provider or model or empty


def ellipsis_truncate(text: str, max_chars: int, *, normalize_ws: bool = False) -> str:
    """Truncate with trailing ellipsis; optionally collapse whitespace first."""

    value = " ".join(text.split()) if normalize_ws else text
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    ellipsis = "..."
    if max_chars <= len(ellipsis):
        return ellipsis[:max_chars]
    return value[: max_chars - len(ellipsis)] + ellipsis
