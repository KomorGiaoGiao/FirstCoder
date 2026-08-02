"""Textual widgets used by the FirstCoder TUI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from textual import events
from textual.await_complete import AwaitComplete
from textual.binding import Binding
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Markdown, Static, TextArea


class _FirstCoderMarkdownBlockSelection:
    """Make a Markdown block follow its owning document's selection state."""

    @property
    def allow_select(self) -> bool:
        parent = getattr(self, "parent", None)
        while parent is not None:
            if isinstance(parent, FirstCoderMarkdown):
                return parent.allow_select
            parent = getattr(parent, "parent", None)
        return False


class _FirstCoderMarkdownDescendantSelection:
    """Make every rendered Markdown helper follow its owning document."""

    @property
    def allow_select(self) -> bool:
        parent = getattr(self, "parent", None)
        while parent is not None:
            if isinstance(parent, FirstCoderMarkdown):
                return parent.allow_select
            parent = getattr(parent, "parent", None)
        return super().allow_select


class FirstCoderMarkdown(Markdown):
    """Markdown output with selection gated by its render lifecycle."""

    ALLOW_SELECT = True

    def __init__(self, *args, selectable: bool = True, **kwargs) -> None:
        self._selectable = selectable
        super().__init__(*args, **kwargs)

    @property
    def allow_select(self) -> bool:
        return self._selectable

    def set_selectable(self, selectable: bool) -> None:
        """Toggle selection once no more Markdown updates will replace blocks."""

        self._selectable = selectable
        self._gate_rendered_descendants()
        self.refresh()

    def _gate_rendered_descendants(self) -> None:
        """Give every mounted Markdown helper a dynamic ancestor selection gate."""

        for descendant in self.query("*"):
            if isinstance(descendant, _FirstCoderMarkdownDescendantSelection):
                continue
            descendant.__class__ = type(
                f"FirstCoder{type(descendant).__name__}",
                (_FirstCoderMarkdownDescendantSelection, type(descendant)),
                {},
            )

    def update(self, markdown: str) -> AwaitComplete:
        """Gate helper widgets created as part of each Markdown render."""

        update_result = super().update(markdown)

        async def update_and_apply_selection_gate() -> None:
            await update_result
            self._gate_rendered_descendants()

        return AwaitComplete(update_and_apply_selection_gate())


FirstCoderMarkdown.BLOCKS = {
    name: type(
        f"FirstCoder{block.__name__}",
        (_FirstCoderMarkdownBlockSelection, block),
        {"ALLOW_SELECT": True},
    )
    for name, block in Markdown.BLOCKS.items()
}


class ComposerTextArea(TextArea):
    """Multiline composer where Enter submits and Shift+Enter inserts a newline."""

    # TextArea owns Ctrl+V, so an App binding is not invoked while the composer
    # has focus. Route the key through this widget to stage clipboard images;
    # terminal Paste events remain responsible for inserting plain text.
    BINDINGS = [
        Binding("ctrl+v", "paste", show=False, priority=True),
        Binding("super+v", "paste", show=False, priority=True),
        Binding("f8", "paste_image", show=False, priority=True),
    ]

    class Submitted(Message):
        pass

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted())
            return
        if event.key == "shift+enter":
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        await super()._on_key(event)

    async def _on_paste(self, event: events.Paste) -> None:
        """Stage pasted files before TextArea inserts their paths as text."""

        stage_attachments = getattr(self.app, "_stage_paste_attachments", None)
        if callable(stage_attachments) and stage_attachments(event.text):
            event.stop()
            event.prevent_default()
            return
        await super()._on_paste(event)

    def action_paste(self) -> None:
        """Attach an OS clipboard image before the terminal emits its Paste event."""

        paste_attachment = getattr(self.app, "_paste_composer_clipboard_image", None)
        if callable(paste_attachment) and paste_attachment():
            return

    def action_paste_image(self) -> None:
        """Attach an OS clipboard image and report when no image is available."""

        paste_attachment = getattr(self.app, "_paste_composer_clipboard_image", None)
        if callable(paste_attachment) and paste_attachment():
            return
        paste_unavailable = getattr(self.app, "_notify_clipboard_image_unavailable", None)
        if callable(paste_unavailable):
            paste_unavailable()


def _plain_static(content: object = "", *args, **kwargs) -> Static:
    kwargs.setdefault("markup", False)
    return Static(content, *args, **kwargs)


def _observe_markdown_update(update_result) -> None:
    future = getattr(update_result, "_future", None)
    if future is None or not hasattr(future, "add_done_callback"):
        return

    def observe_cancelled_update(done_future) -> None:
        try:
            exception = done_future.exception()
        except asyncio.CancelledError:
            return
        if isinstance(exception, asyncio.CancelledError):
            return
        if exception is not None:
            raise exception

    future.add_done_callback(observe_cancelled_update)


@dataclass(slots=True)
class FirstCoderTuiConfig:
    title: str = "FirstCoder"
    provider_name: str | None = None
    provider_model: str | None = None
    project_name: str | None = None


class FirstCoderScreen(Screen[None]):
    """Notify the app after Textual has committed a new terminal size."""

    def _screen_resized(self, size) -> None:
        super()._screen_resized(size)
        callback = getattr(self.app, "_on_terminal_resized", None)
        if callback is not None:
            callback()
