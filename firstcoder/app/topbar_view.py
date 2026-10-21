"""FirstCoder TUI 顶栏的 markup 助手。

PERMISSION_MODE_COLORS: 三种权限模式的顶栏配色, bypass 用警示红。
"""

from __future__ import annotations

from rich.markup import escape
from rich.text import Text

from firstcoder.app import yuren_topbar_themes

PERMISSION_MODE_COLORS = {
    "standard": "#cfd1d6",
    "aggressive": "#f6b73c",
    "bypass": "#ff6b5f",
}


def _markup_width(markup: str) -> int:
    return len(Text.from_markup(markup).plain)


def _truncate_markup(markup: str, width: int) -> str:
    """把带样式的 markup 截到一行终端宽度内。"""
    text = Text.from_markup(markup)
    text.truncate(max(0, width), overflow="ellipsis", pad=False)
    return text.markup


def _metadata_markup(values: list[tuple[str | None, str, int | None]], *, separator: str) -> str:
    return separator.join(value if color is None else f"[{color}]{escape(value)}[/]" for color, value, _ in values)


def _provider_name_markup(provider: str, *, glow_frame: int = 0) -> str:
    """渲染普通(非彩蛋) provider 标签。"""
    return f"[#7bba55]{escape(provider)}[/]"


def _provider_model_markup(provider: str, model: str, *, glow_frame: int = 0) -> str:
    """渲染 provider/model 标签; 命中 Yuren 彩蛋时返回逐字流光版本。"""
    themed = yuren_topbar_themes.provider_model_markup(
        provider,
        model,
        glow_frame=glow_frame,
    )
    if themed is not None:
        return themed
    return f"{_provider_name_markup(provider, glow_frame=glow_frame)}[#6e6d72]/{escape(model)}[/]"
