"""FirstCoder TUI 用到的 Textual widgets。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast

from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.screen import Screen
from textual.visual import VisualType
from textual.widgets import Markdown, Static, TextArea


class FirstCoderMarkdown(Markdown):
    """可选中状态随渲染生命周期门控的 Markdown 输出。

    流式更新期间 selectable=False, 配合 FirstCoderScreen 在选区命中时返回
    (None, None) 封锁选择, 避免选中框跟着内容增长跳动; 段关闭后由
    _finalize_stream_widget 调 set_selectable(True) 放开。
    """

    ALLOW_SELECT = True

    def __init__(self, *args, selectable: bool = True, **kwargs) -> None:
        self._selectable = selectable
        super().__init__(*args, **kwargs)

    @property
    def allow_select(self) -> bool:
        return self._selectable

    def set_selectable(self, selectable: bool) -> None:
        """确认不再有 Markdown 更新会替换 block 后, 才允许切换选中状态。"""

        self._selectable = selectable
        self.refresh()


# Textual 的 Markdown 把各类 block 换成子类渲染; 这里动态子类化全部 block,
# 让它们也带上 ALLOW_SELECT, 否则块级元素整段不可选。
FirstCoderMarkdown.BLOCKS = {
    name: type(
        f"FirstCoder{block.__name__}",
        (block,),
        {"ALLOW_SELECT": True},
    )
    for name, block in Markdown.BLOCKS.items()
}


class ComposerTextArea(TextArea):
    """多行输入框: Enter 提交(发 Submitted 消息), Shift+Enter 换行。

    TextArea 自己吃掉了 Ctrl+V, App 级 binding 在输入框聚焦时不会触发, 所以
    在这个 widget 上声明绑定并路由给 app 暂存剪贴板图片; 终端的 Paste 事件
    仍负责插入普通文本。
    """
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
        """先把粘贴内容交给 app 暂存为附件, 消费不了再让 TextArea 当文本插入。"""

        stage_attachments = getattr(self.app, "_stage_paste_attachments", None)
        if callable(stage_attachments) and stage_attachments(event.text):
            event.stop()
            event.prevent_default()
            return
        await super()._on_paste(event)

    def action_paste(self) -> None:
        """终端发出 Paste 事件前, 先尝试把系统剪贴板图片挂为附件。"""

        paste_attachment = getattr(self.app, "_paste_composer_clipboard_image", None)
        if callable(paste_attachment) and paste_attachment():
            return

    def action_paste_image(self) -> None:
        """显式粘贴图片(F8): 剪贴板无图片时给出提示。"""

        paste_attachment = getattr(self.app, "_paste_composer_clipboard_image", None)
        if callable(paste_attachment) and paste_attachment():
            return
        paste_unavailable = getattr(self.app, "_notify_clipboard_image_unavailable", None)
        if callable(paste_unavailable):
            paste_unavailable()


def _plain_static(content: object = "", *args, **kwargs) -> Static:
    # 默认 markup=False: 输出区内容多为模型/工具文本, 不应当被当作 Rich markup 解析。
    kwargs.setdefault("markup", False)
    return Static(cast(VisualType, content), *args, **kwargs)


def _observe_markdown_update(update_result) -> None:
    """观察 Markdown.update 的 future: 取消(流式被打断)静默忽略, 真异常上抛。"""

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
    """默认 Screen: 尺寸变化通知 app 刷顶栏, 并封锁流式 Markdown 上的选区。"""

    @staticmethod
    def _selection_is_blocked_by_streaming_markdown(widget) -> bool:
        """叶子 widget 属于仍在更新的 Markdown 文档时, 拒绝其参与选区。"""

        parent = widget
        while parent is not None:
            if isinstance(parent, FirstCoderMarkdown) and not parent.allow_select:
                return True
            parent = getattr(parent, "parent", None)
        return False

    def get_widget_and_offset_at(self, x: int, y: int):
        widget, offset = super().get_widget_and_offset_at(x, y)
        if widget is not None and self._selection_is_blocked_by_streaming_markdown(widget):
            return None, None
        return widget, offset

    def _screen_resized(self, size) -> None:
        super()._screen_resized(size)
        callback = getattr(self.app, "_on_terminal_resized", None)
        if callback is not None:
            callback()
