"""Yuren topbar easter-egg themes.

Keep this optional chrome effect isolated from the main TUI so palette and match
rules can evolve or be removed without touching general provider rendering.

Trigger rule: provider display name must be exactly "Yuren", then model id must
match an entry in MODEL_GLOW_PALETTES.
"""

from __future__ import annotations

from rich.markup import escape

PROVIDER_NAME = "Yuren"
GLOW_INTERVAL_SECONDS = 0.18
MODEL_GLOW_PALETTES: dict[str, tuple[str, ...]] = {
    # GPT-5.6 celestial trio
    "gpt-5.6-terra": ("#b8ffdf", "#81e8bb", "#18cfcb", "#45e6df", "#5fb5ff"),
    "gpt-5.6-sol": ("#ffb347", "#ff7a45", "#ff5c3d", "#e9422e", "#ffd166"),
    "gpt-5.6-luna": ("#f4f6ff", "#c9d5ff", "#b9c8ff", "#a99ee8", "#d9e7ff"),
    # GPT mainline ladder
    "gpt-5.5": ("#e8f1ff", "#b9d4ff", "#7eb6ff", "#4f8cff", "#9aa4b2"),
    "gpt-5.4": ("#dff7ff", "#9edfff", "#57c5f0", "#3aa7d6", "#6ec8b8"),
    "gpt-5.4-mini": ("#f0fff8", "#c9f5e1", "#9be7c8", "#6fd4d0", "#8ec5ff"),
    # Grok / Fable
    "grok-4.5": ("#f0e7ff", "#d2b4ff", "#b57bff", "#8b5cf6", "#5ce1ff"),
    "fable-5": ("#ffe9bf", "#ffc857", "#f0a05a", "#e07850", "#c7a0ff"),
    # Opus / Sonnet warm family
    "opus-4-7": ("#ffe0d1", "#ffb899", "#ff8f6b", "#e86a4a", "#f4c27a"),
    "opus-4-8": ("#ffd6e0", "#ff9aa8", "#ff6f61", "#d94848", "#ffc36b"),
    "sonnet-5": ("#fff0d8", "#ffd29a", "#f0b36a", "#d7925a", "#e8c4a2"),
    "sonnet-4-6": ("#ffe8ef", "#ffc2d1", "#f0a18c", "#d9896c", "#f2d0a8"),
}


def model_glow_palette(provider: str, model: str) -> tuple[str, ...] | None:
    """Return the animated palette for a supported Yuren model, else None."""

    if provider != PROVIDER_NAME:
        return None
    return MODEL_GLOW_PALETTES.get(model)


def should_animate(provider: str, model: str) -> bool:
    return model_glow_palette(provider, model) is not None


def provider_model_markup(provider: str, model: str, *, glow_frame: int = 0) -> str | None:
    """Return Yuren glow markup when the easter egg applies; otherwise None."""

    palette = model_glow_palette(provider, model)
    if palette is None:
        return None
    return f"{glow_markup(provider, glow_frame=glow_frame, palette=palette)}" f"[#6e6d72]/[/]" f"{glow_markup(model, glow_frame=glow_frame + len(provider) + 1, palette=palette)}"


def glow_markup(text: str, *, glow_frame: int, palette: tuple[str, ...]) -> str:
    return "".join(f"[{palette[(index + glow_frame) % len(palette)]}]" f"{escape(character)}[/]" for index, character in enumerate(text))
