"""Textual widgets used by the FirstCoder TUI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Markdown, Static, TextArea


class FirstCoderMarkdown(Markdown):
    """Markdown output that avoids Textual's fragile selection path."""

    ALLOW_SELECT = False
    BLOCKS = {name: type(f"FirstCoder{block.__name__}", (block,), {"ALLOW_SELECT": False}) for name, block in Markdown.BLOCKS.items()}


class ComposerTextArea(TextArea):
    """Multiline composer where Enter submits and Shift+Enter inserts a newline."""

    # TextArea owns Ctrl+V, so an App binding is not invoked while the composer
    # has focus. Route the key through this widget before falling back to the
    # regular text-paste behavior.
    BINDINGS = [
        Binding("ctrl+v", "paste", show=False, priority=True),
        Binding("super+v", "paste", show=False, priority=True),
        Binding("f8", "paste", show=False, priority=True),
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
        """Attach an OS clipboard image, otherwise retain TextArea text paste."""

        paste_attachment = getattr(self.app, "_paste_composer_clipboard_image", None)
        if paste_attachment is not None and paste_attachment():
            return
        paste_unavailable = getattr(self.app, "_notify_clipboard_image_unavailable", None)
        if callable(paste_unavailable):
            paste_unavailable()
        super().action_paste()


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
