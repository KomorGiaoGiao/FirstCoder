"""Topbar markup helpers for the FirstCoder TUI."""

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
    """Return styled markup constrained to one terminal row."""
    text = Text.from_markup(markup)
    text.truncate(max(0, width), overflow="ellipsis", pad=False)
    return text.markup


def _metadata_markup(values: list[tuple[str | None, str, int | None]], *, separator: str) -> str:
    return separator.join(value if color is None else f"[{color}]{escape(value)}[/]" for color, value, _ in values)


def _provider_name_markup(provider: str, *, glow_frame: int = 0) -> str:
    """Render the provider-only part for ordinary, non-easter-egg labels."""
    return f"[#7bba55]{escape(provider)}[/]"


def _provider_model_markup(provider: str, model: str, *, glow_frame: int = 0) -> str:
    """Render provider/model labels, applying the optional Yuren easter egg."""
    themed = yuren_topbar_themes.provider_model_markup(
        provider,
        model,
        glow_frame=glow_frame,
    )
    if themed is not None:
        return themed
    return f"{_provider_name_markup(provider, glow_frame=glow_frame)}[#6e6d72]/{escape(model)}[/]"
