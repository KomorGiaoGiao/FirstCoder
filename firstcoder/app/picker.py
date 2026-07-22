"""Reusable TUI picker state and rendering helpers."""

from __future__ import annotations

from firstcoder.utils.text import ellipsis_truncate

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True, slots=True)
class TuiPickerItem:
    id: str
    label: str
    detail: str = ""
    meta: dict[str, str] | None = None


@dataclass(slots=True)
class TuiPickerState:
    kind: str
    title: str
    items: list[TuiPickerItem]
    selected_index: int = 0
    empty_text: str = "No items."
    footer: str = "Use up/down and enter to select."
    count_label: str = "items"

    def move(self, delta: int) -> None:
        if not self.items:
            self.selected_index = 0
            return
        self.selected_index = max(0, min(len(self.items) - 1, self.selected_index + delta))

    @property
    def selected_item(self) -> TuiPickerItem | None:
        if not self.items:
            return None
        index = max(0, min(self.selected_index, len(self.items) - 1))
        return self.items[index]


def visible_picker_window(items: list[TuiPickerItem], *, selected_index: int, limit: int) -> tuple[int, list[TuiPickerItem]]:
    if not items:
        return 0, []
    selected_index = max(0, min(selected_index, len(items) - 1))
    limit = max(1, limit)
    if len(items) <= limit:
        return 0, items
    window_start = min(max(0, selected_index - limit + 1), len(items) - limit)
    return window_start, items[window_start : window_start + limit]


def render_picker(
    picker: TuiPickerState,
    *,
    limit: int,
    render_item: Callable[[TuiPickerItem, int], str] | None = None,
) -> str:
    if not picker.items:
        return picker.empty_text
    window_start, visible_items = visible_picker_window(picker.items, selected_index=picker.selected_index, limit=limit)
    lines = [_picker_header(picker.title, window_start, len(visible_items), len(picker.items), picker.count_label)]
    for offset, item in enumerate(visible_items):
        index = window_start + offset
        marker = ">" if index == picker.selected_index else " "
        body = render_item(item, index) if render_item is not None else _default_item_text(item)
        lines.append(f"{marker} {index + 1}. {body}")
    selected = picker.selected_item
    if picker.kind == "skill" and selected is not None and selected.detail:
        lines.append(f"Selected: {ellipsis_truncate(selected.detail, 140, normalize_ws=True)}")
    if picker.footer:
        lines.append(picker.footer)
    return "\n".join(lines)


def _picker_header(title: str, window_start: int, visible_count: int, total_count: int, count_label: str) -> str:
    if total_count <= visible_count:
        return title
    window_end = window_start + visible_count
    return f"{title} Showing {window_start + 1}-{window_end} of {total_count} {count_label}"


def _default_item_text(item: TuiPickerItem) -> str:
    if item.detail:
        return f"{item.label} {item.detail}"
    return item.label
