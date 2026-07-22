"""Command-specific adapters for the reusable TUI picker."""

from __future__ import annotations

from firstcoder.app.picker import TuiPickerItem, TuiPickerState


def session_picker_item(item: dict[str, object]) -> TuiPickerItem:
    session_id = str(item.get("session_id") or "")
    title = str(item.get("title") or "")
    message_count = item.get("message_count")
    return TuiPickerItem(
        id=session_id,
        label=f"{session_id} {title}".strip(),
        detail=f"messages={message_count}",
    )


def model_picker_item(item: dict[str, object]) -> TuiPickerItem:
    provider = str(item.get("provider") or "")
    model = str(item.get("model") or "")
    spec = f"{provider}/{model}" if provider else model
    return TuiPickerItem(id=spec, label=spec)


def skill_picker_item(item: dict[str, object]) -> TuiPickerItem:
    name = str(item.get("name") or "")
    path = str(item.get("path") or "")
    scope = str(item.get("scope") or "")
    description = str(item.get("description") or "")
    return TuiPickerItem(
        id=name,
        label=name or path,
        detail=description,
        meta={"scope": scope, "path": path},
    )


def picker_command(kind: str, item: TuiPickerItem) -> str | None:
    if kind == "resume":
        return f"/resume {item.id}" if item.id else None
    if kind == "model":
        return f"/model {item.id}" if item.id else None
    if kind == "skill":
        return f"/skill-use {item.id}" if item.id else None
    return None


def render_picker_item(picker: TuiPickerState, item: TuiPickerItem, index: int) -> str:
    if picker.kind != "skill":
        return f"{item.label} {item.detail}".strip()
    meta = item.meta or {}
    path = meta.get("path") or item.id
    scope = meta.get("scope") or "-"
    lines = [item.label]
    lines.append(f"    {scope} · {path}")
    return "\n".join(lines)
