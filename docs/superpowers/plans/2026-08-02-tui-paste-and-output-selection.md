# FirstCoder TUI 粘贴与输出选择复制修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Composer 普通文本粘贴两次的问题，并在不重新引入流式 Markdown 更新崩溃的前提下，让已稳定的终端输出支持鼠标选择和复制。

**Architecture:** 把终端的 bracketed-paste `events.Paste` 设为普通文本唯一写入通道；`Ctrl/Cmd+V` 只探测系统剪贴板图片，不再从 Textual 的应用内 clipboard 二次插入文本，`F8` 保留为显式图片粘贴入口。输出选择按生命周期开启：普通 `Static` 和已完成 Markdown 默认可选，仍在 `Markdown.update()` 的流式实例及其 Block 暂时不可选，最后一次更新完成后再原地开放；App 的 `Ctrl+C` 改为“有输出选区则复制、无输出选区则退出”。

**Tech Stack:** Python、Textual 8.2.8、pytest/pytest-anyio

---

## 已确认的故障边界

- `ComposerTextArea.action_paste()` 当前先探测图片，未命中后调用 `TextArea.action_paste()`，后者读取 `app.clipboard` 并插入一次；真实终端随后发送 `events.Paste`，`ComposerTextArea._on_paste()` 又委托 Textual 插入一次，所以出现 `hellohello`。
- `app.clipboard` 是 Textual 的应用内 clipboard，不是操作系统剪贴板的权威来源；图片仍应由现有 `resolve_paste_attachments(None)` 路径探测。
- `FirstCoderApp.ALLOW_SELECT`、`FirstCoderMarkdown.ALLOW_SELECT` 和 `FirstCoderMarkdown.BLOCKS` 当前三层均为 `False`，输出自然无法进入 Textual 的选择路径。
- `FirstCoderApp.BINDINGS` 把 `Ctrl+C` 直接绑定为 `quit`，会覆盖 Screen 的 `ctrl+c,super+c -> screen.copy_text` 输出复制路径。
- Textual 最新正式版与当前环境均为 8.2.8。升级依赖不是本修复的组成部分；修复 FirstCoder 的事件所有权和选择生命周期即可。

## 文件与职责

- 修改 `firstcoder/app/tui_widgets.py:15-69`：区分普通粘贴事件与图片快捷键；为 Markdown 及其 Block 提供实例级可选择状态。
- 修改 `firstcoder/app/tui.py:84-88, 793-821`：恢复 App 选择能力，协调 `Ctrl+C` 的复制/退出语义，在最终响应处封闭流式选择生命周期。
- 修改 `firstcoder/app/tui_view.py:490-499, 651-738`：稳定消息默认可选；流式消息在更新期间禁用选择，并在对应 widget 的最终更新完成后开放。
- 修改 `tests/test_app_tui.py:540-560, 1160-1230, 1580-1620, 1630-1830`：增加真实双通道粘贴、选择复制、右键安全和流式状态转换回归测试。

## Task 1：让普通文本只有一个粘贴写入通道

**Files:**

- Modify: `firstcoder/app/tui_widgets.py:22-69`
- Test: `tests/test_app_tui.py:1160-1230`

- [ ] **Step 1：先写能复现真实双通道的失败测试**

在 `tests/test_app_tui.py` 的现有粘贴测试旁新增以下用例。它先触发 FirstCoder 的快捷键 action，再投递终端 `Paste` 事件；修复前最终文本会是 `hellohello`。

```python
@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("paste_key", ["ctrl+v", "super+v"])
async def test_plain_text_paste_is_inserted_once_when_key_and_terminal_event_both_arrive(
    monkeypatch,
    paste_key,
) -> None:
    monkeypatch.setattr("firstcoder.app.tui.resolve_paste_attachments", lambda text: [])
    app = FirstCoderApp()

    async with app.run_test() as pilot:
        composer = app.query_one("#input", ComposerTextArea)
        app.clipboard = "hello"
        await pilot.click("#input")
        await pilot.press(paste_key)
        await composer._on_paste(events.Paste("hello"))

        assert composer.text == "hello"

    assert app._staged_attachments == []
```

- [ ] **Step 2：运行测试并确认 Red 的原因准确**

Run:

```bash
.venv/bin/python -m pytest tests/test_app_tui.py::test_plain_text_paste_is_inserted_once_when_key_and_terminal_event_both_arrive -q
```

Expected: 两个参数用例均失败，差异为实际值 `hellohello`、期望值 `hello`。如果失败原因不是重复插入，停止实现并重新检查事件序列。

- [ ] **Step 3：把快捷键 action 收窄为图片探测，不再插入普通文本**

在 `firstcoder/app/tui_widgets.py` 中保留 `Ctrl/Cmd+V` 的优先级绑定以便先处理图片，但移除 `super().action_paste()`；将 `F8` 拆成显式图片 action，只有它在未命中图片时显示提示。普通文本只由现有 `_on_paste()` 委托 `TextArea._on_paste(event)` 插入。

```python
class ComposerTextArea(TextArea):
    BINDINGS = [
        Binding("ctrl+v", "paste", show=False, priority=True),
        Binding("super+v", "paste", show=False, priority=True),
        Binding("f8", "paste_image", show=False, priority=True),
    ]

    def _paste_clipboard_image(self) -> bool:
        paste_attachment = getattr(self.app, "_paste_composer_clipboard_image", None)
        return bool(paste_attachment is not None and paste_attachment())

    def action_paste(self) -> None:
        """Probe for an OS clipboard image; terminal Paste owns plain text."""

        self._paste_clipboard_image()

    def action_paste_image(self) -> None:
        """Explicit image-paste command used by F8."""

        if self._paste_clipboard_image():
            return
        paste_unavailable = getattr(self.app, "_notify_clipboard_image_unavailable", None)
        if callable(paste_unavailable):
            paste_unavailable()
```

不要改 `_on_paste()` 的附件优先规则：文件路径命中附件时继续 `stop()` + `prevent_default()`；普通文本未命中时继续调用 `super()._on_paste(event)`，且只调用一次。

- [ ] **Step 4：更新快捷键行为测试，锁定静默普通粘贴与 F8 提示**

把现有 `test_firstcoder_app_paste_shortcut_reports_missing_clipboard_image_while_composer_is_focused` 拆成下面两个明确契约；避免普通 `Ctrl/Cmd+V` 每次都向 transcript 写“No clipboard image found”。

```python
@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("paste_key", ["ctrl+v", "super+v"])
async def test_plain_text_paste_shortcut_does_not_report_missing_image(
    monkeypatch,
    paste_key,
) -> None:
    monkeypatch.setattr("firstcoder.app.tui.resolve_paste_attachments", lambda text: [])
    app = FirstCoderApp()

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(paste_key)
        assert "No clipboard image found" not in _static_output_text(app)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_f8_reports_missing_clipboard_image(monkeypatch) -> None:
    monkeypatch.setattr("firstcoder.app.tui.resolve_paste_attachments", lambda text: [])
    app = FirstCoderApp()

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press("f8")
        assert "No clipboard image found" in _static_output_text(app)
```

保留并继续参数化运行 `test_firstcoder_app_paste_shortcut_stages_clipboard_image_while_composer_is_focused` 的 `ctrl+v`、`super+v`、`f8` 三种图片路径。

- [ ] **Step 5：运行粘贴测试并确认 Green**

Run:

```bash
.venv/bin/python -m pytest tests/test_app_tui.py -q -k 'paste or clipboard_image'
```

Expected: 新增双通道测试、普通文本、文件路径、系统剪贴板图片和 F8 提示测试全部通过。

- [ ] **Step 6：提交独立的粘贴修复切片**

```bash
git add firstcoder/app/tui_widgets.py tests/test_app_tui.py
git diff --cached --check
git commit -m "Fix duplicate composer paste"
```

## Task 2：恢复稳定输出选择，并解决 Ctrl+C 复制与退出冲突

**Files:**

- Modify: `firstcoder/app/tui_widgets.py:15-19`
- Modify: `firstcoder/app/tui.py:84-88`
- Test: `tests/test_app_tui.py:540-560, 1580-1620`

- [ ] **Step 1：先把旧的“永不选择”断言改成稳定输出可选择的失败测试**

用以下测试替换 `test_firstcoder_markdown_does_not_enter_textual_selection_path` 和 Block 的全 `False` 断言。测试明确覆盖父 Markdown 和段落、列表、代码块对应的动态 Block 类型。

```python
def test_stable_firstcoder_markdown_and_blocks_allow_selection() -> None:
    markdown = FirstCoderMarkdown()

    assert markdown.allow_select is True
    assert FirstCoderMarkdown.BLOCKS
    assert all(block.ALLOW_SELECT is True for block in FirstCoderMarkdown.BLOCKS.values())


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_static_and_completed_markdown_output_allow_selection() -> None:
    app = FirstCoderApp()

    async with app.run_test() as pilot:
        app._write_line("plain output", kind=TuiEntryKind.SYSTEM)
        app._write_markdown_message("paragraph\n\n- list\n\n```python\nprint('ok')\n```")
        await pilot.pause()

        assert app.ALLOW_SELECT is True
        assert all(widget.allow_select for widget in app.query("#output Static"))
        markdown = app.query_one("FirstCoderMarkdown", FirstCoderMarkdown)
        assert markdown.allow_select is True
        assert all(block.allow_select for block in markdown.query("*"))
```

- [ ] **Step 2：运行选择测试并确认 Red**

Run:

```bash
.venv/bin/python -m pytest tests/test_app_tui.py -q -k 'stable_firstcoder_markdown or static_and_completed_markdown'
```

Expected: 因 App、Markdown 或 Block 的 `allow_select` 仍为 `False` 而失败。

- [ ] **Step 3：最小恢复 App、稳定 Markdown 和 Block 的 Textual 原生选择能力**

在 `firstcoder/app/tui_widgets.py` 恢复稳定 Markdown 默认值，不增加自制鼠标选择或终端转义序列：

```python
class FirstCoderMarkdown(Markdown):
    """Markdown output with selection gated by render lifecycle."""

    ALLOW_SELECT = True
    BLOCKS = {
        name: type(f"FirstCoder{block.__name__}", (block,), {"ALLOW_SELECT": True})
        for name, block in Markdown.BLOCKS.items()
    }
```

在 `firstcoder/app/tui.py` 恢复全局选择入口，并把退出 action 改名以便下一步加入条件分流：

```python
class FirstCoderApp(FirstCoderViewMixin, App[None]):
    ALLOW_SELECT = True
    BINDINGS = [
        ("ctrl+c", "copy_output_or_quit", "Copy output or quit"),
    ]
```

- [ ] **Step 4：先写 Ctrl+C 分流的失败测试**

测试 action 本身，不依赖操作系统 clipboard。用 monkeypatch 固定 Screen 的输出选区结果，并观察调用了复制还是退出：

```python
@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_ctrl_c_copies_when_screen_has_selected_output(monkeypatch) -> None:
    app = FirstCoderApp()
    copied: list[str] = []
    quit_calls: list[bool] = []

    async with app.run_test():
        monkeypatch.setattr(app.screen, "get_selected_text", lambda: "selected output")
        monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
        monkeypatch.setattr(app, "action_quit", lambda: quit_calls.append(True))
        app.action_copy_output_or_quit()

    assert copied == ["selected output"]
    assert quit_calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_ctrl_c_quits_when_screen_has_no_selected_output(monkeypatch) -> None:
    app = FirstCoderApp()
    quit_calls: list[bool] = []

    async with app.run_test():
        monkeypatch.setattr(app.screen, "get_selected_text", lambda: None)
        monkeypatch.setattr(app, "action_quit", lambda: quit_calls.append(True))
        app.action_copy_output_or_quit()

    assert quit_calls == [True]
```

再增加 Composer 聚焦时的键盘回归，明确输入框的原生复制优先于 App 的退出 action：

```python
@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("copy_key", ["ctrl+c", "super+c"])
async def test_composer_selection_copy_does_not_quit(copy_key) -> None:
    app = FirstCoderApp()

    async with app.run_test() as pilot:
        composer = app.query_one("#input", ComposerTextArea)
        composer.load_text("copy me")
        composer.selection = ((0, 0), (0, 7))
        await pilot.click("#input")
        await pilot.press(copy_key)

        assert app.clipboard == "copy me"
        assert app.is_running is True
```

- [ ] **Step 5：实现 Ctrl+C 的条件复制；保留 Composer 自己的文本复制**

在 `FirstCoderApp` 中增加：

```python
def action_copy_output_or_quit(self) -> None:
    """Copy focused input or selected output, otherwise retain Ctrl+C quit."""

    focused = self.focused
    if isinstance(focused, TextArea) and focused.selected_text:
        focused.action_copy()
        return
    selected_text = self.screen.get_selected_text()
    if selected_text is not None:
        self.copy_to_clipboard(selected_text)
        return
    self.action_quit()
```

不绑定 `super+c`：让 Textual Screen 继续处理 `Cmd+C` 输出复制。`Ctrl+C` 到达 App action 时先检查 Composer 的 `selected_text`，因此输入框复制不会退出；输出有选区时复制输出；两处都无选区时才退出。

- [ ] **Step 6：把右键防崩溃测试改为“可选择且不崩溃”**

将现有两个右键测试中的旧断言改为：

```python
markdown = app.query_one("FirstCoderMarkdown", FirstCoderMarkdown)
assert app.ALLOW_SELECT is True
assert markdown.allow_select is True
assert all(block.allow_select for block in markdown.query("*"))
await pilot.click(markdown, button=3)
await pilot.pause()
```

两个场景都必须保留：普通 Markdown 响应，以及 fenced code block。右键测试负责证明恢复选择后不会重新触发旧崩溃，而不是继续绕开选择路径。

- [ ] **Step 7：运行稳定输出与复制分流测试并确认 Green**

Run:

```bash
.venv/bin/python -m pytest tests/test_app_tui.py -q -k 'select or copy_output_or_quit or right_clicking_markdown'
```

Expected: 稳定 `Static`、普通段落、列表、代码块可选择；有输出选区时复制，无选区时 `Ctrl+C` 退出；右键不崩溃。

## Task 3：流式 Markdown 更新期间禁选，稳定后开放

**Files:**

- Modify: `firstcoder/app/tui_widgets.py:15-19`
- Modify: `firstcoder/app/tui.py:793-821`
- Modify: `firstcoder/app/tui_view.py:651-738`
- Test: `tests/test_app_tui.py:1630-1830`

- [ ] **Step 1：先写流式父 widget 与 Block 生命周期的失败测试**

新增一个挂载后的父子状态测试和一个 App 流式测试。前者用真实段落、列表和 fenced code Block 锁定父子同步，后者锁定“更新中不可选、最终更新完成后可选”。

```python
@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_streaming_markdown_selection_state_applies_to_mounted_blocks() -> None:
    app = FirstCoderApp()

    async with app.run_test() as pilot:
        output = app.query_one("#output")
        markdown = FirstCoderMarkdown(selectable=False)
        await output.mount(markdown)
        await markdown.update("paragraph\n\n- list\n\n```python\nprint('ok')\n```")
        await pilot.pause()

        blocks = list(markdown.query("*"))
        assert blocks
        assert markdown.allow_select is False
        assert all(block.allow_select is False for block in blocks)

        markdown.set_selectable(True)

        assert markdown.allow_select is True
        assert all(block.allow_select is True for block in blocks)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_streaming_markdown_becomes_selectable_only_after_final_update() -> None:
    app = FirstCoderApp()

    async with app.run_test() as pilot:
        app._append_stream_text("partial")
        markdown = app.query_one("FirstCoderMarkdown.streaming", FirstCoderMarkdown)
        assert markdown.allow_select is False
        assert all(block.allow_select is False for block in markdown.query("*"))

        app._stream_text_buffer = "final answer"
        app._write_chat_response(ChatResponse(provider="fake", model="fake", content="final answer"))
        assert markdown.allow_select is False

        await pilot.pause()
        assert markdown.allow_select is True
        assert all(block.allow_select is True for block in markdown.query("*"))
```

不能只断言父 Markdown，因为鼠标实际命中的是挂载后的子 Block。

- [ ] **Step 2：运行流式选择测试并确认 Red**

Run:

```bash
.venv/bin/python -m pytest tests/test_app_tui.py -q -k 'streaming_markdown_selection_state or streaming_markdown_becomes_selectable'
```

Expected: `FirstCoderMarkdown` 尚不接受 `selectable`，或流式 widget 从创建起就保持可选，测试失败。

- [ ] **Step 3：实现 Markdown 实例级选择状态，并让 Block 动态跟随祖先**

在 `firstcoder/app/tui_widgets.py` 中用一个 Block mixin 替代固定 `ALLOW_SELECT=False` 子类。Block 的 `allow_select` 每次读取其祖先 `FirstCoderMarkdown` 的当前状态，所以最后一次 update 完成后无需销毁、重建 Block：

```python
class _FirstCoderMarkdownBlockSelection:
    @property
    def allow_select(self) -> bool:
        parent = self.parent
        while parent is not None:
            if isinstance(parent, FirstCoderMarkdown):
                return parent.allow_select
            parent = parent.parent
        return False


class FirstCoderMarkdown(Markdown):
    ALLOW_SELECT = True

    def __init__(self, *args, selectable: bool = True, **kwargs) -> None:
        self._selectable = selectable
        super().__init__(*args, **kwargs)

    @property
    def allow_select(self) -> bool:
        return self._selectable

    def set_selectable(self, selectable: bool) -> None:
        self._selectable = selectable
        self.refresh()


FirstCoderMarkdown.BLOCKS = {
    name: type(
        f"FirstCoder{block.__name__}",
        (_FirstCoderMarkdownBlockSelection, block),
        {},
    )
    for name, block in Markdown.BLOCKS.items()
}
```

如果 Textual Block 的 `parent` 在未挂载时不可访问，mixin 对 `getattr(self, "parent", None)` 返回 `None`；实际契约以挂载后的 Block 测试为准。

- [ ] **Step 4：创建流式 widget 时禁选，并记录“该 widget 不再更新”**

在 `firstcoder/app/tui_view.py::_append_stream_text()` 创建处改为：

```python
self._stream_text_widget = FirstCoderMarkdown(
    classes="message assistant-message streaming",
    selectable=False,
)
```

增加一个只对特定 widget 生效的收尾 helper。它接管该 widget 当前在途的 update，等它完成后再提交最终 snapshot，最终 snapshot 完成后才开放选择；所有 callback 都捕获局部 `widget`，不会误伤工具调用后的下一段流式输出：

```python
def _enable_stream_widget_selection(self, widget: FirstCoderMarkdown) -> None:
    if widget.is_attached:
        widget.set_selectable(True)


def _update_closed_stream_widget(
    self,
    widget: FirstCoderMarkdown,
    final_markdown: str,
) -> None:
    update_result = widget.update(final_markdown)
    _observe_markdown_update(update_result)
    future = getattr(update_result, "_future", None)
    if future is None or not hasattr(future, "add_done_callback"):
        self._enable_stream_widget_selection(widget)
        return

    def enable_after_final_update(_future) -> None:
        self._schedule_ui_callback(self._enable_stream_widget_selection, widget)

    future.add_done_callback(enable_after_final_update)


def _finalize_stream_widget(self, widget: FirstCoderMarkdown, final_text: str) -> None:
    final_markdown = f"FirstCoder:\n\n{final_text}"
    pending_update = self._stream_markdown_update
    rendered_text = self._stream_rendered_text
    self._stream_markdown_update = None
    self._stream_rendered_text = final_text

    future = getattr(pending_update, "_future", None)
    if future is not None and hasattr(future, "add_done_callback"):
        def finish_pending_update(_future) -> None:
            callback = (
                self._enable_stream_widget_selection
                if rendered_text == final_text
                else self._update_closed_stream_widget
            )
            args = (widget,) if rendered_text == final_text else (widget, final_markdown)
            self._schedule_ui_callback(callback, *args)

        future.add_done_callback(finish_pending_update)
    elif rendered_text == final_text:
        self._enable_stream_widget_selection(widget)
    else:
        self._update_closed_stream_widget(widget, final_markdown)
```

在两个确定 widget 不会再接收 delta 的边界调用 helper：

- `_write_chat_response()` 已把最终 `content` 合入 `_stream_text_buffer` 后，以局部变量保存当前 widget 并调用 `_finalize_stream_widget(widget, self._stream_text_buffer)`；此路径不再调用 `_flush_stream_text()`。
- `_close_stream_segment_for_tool()` drain 完 delta 后，以局部变量保存当前 widget 并调用 `_finalize_stream_widget(widget, self._stream_text_buffer)`，再把 `_stream_segment_closed_for_tool=True`。

调用前取消待触发的 `_stream_flush_timer` 并将其清空，避免封闭后的 timer 再更新旧 widget：

```python
widget = self._stream_text_widget
timer = self._stream_flush_timer
if timer is not None:
    timer.stop()
self._stream_flush_timer = None
if widget is not None:
    self._finalize_stream_widget(widget, self._stream_text_buffer)
```

保留普通流式阶段现有 `_flush_stream_text()`、`_track_stream_markdown_update()` backpressure 和 `_observe_markdown_update()` 异常观察逻辑。收尾 helper 把全局 `_stream_markdown_update` 清空，使旧的 `_finish_stream_markdown_update()` callback 因 identity 不匹配而安全返回，再由捕获局部 widget 的 callback 完成最终 snapshot。

- [ ] **Step 5：增加多段流式输出回归，防止工具边界前的旧段永久禁选**

```python
@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_stream_segments_on_both_sides_of_tool_become_selectable() -> None:
    app = FirstCoderApp()

    async with app.run_test() as pilot:
        app._append_stream_text("before tool")
        first = app.query_one("FirstCoderMarkdown.streaming", FirstCoderMarkdown)
        app._close_stream_segment_for_tool()
        await pilot.pause()
        assert first.allow_select is True

        app._append_stream_text("after tool")
        widgets = list(app.query("FirstCoderMarkdown.streaming"))
        second = widgets[-1]
        assert second is not first
        assert second.allow_select is False

        app._write_chat_response(
            ChatResponse(provider="fake", model="fake", content="after tool")
        )
        await pilot.pause()
        assert second.allow_select is True
```

- [ ] **Step 6：运行所有流式 Markdown 回归并确认 Green**

Run:

```bash
.venv/bin/python -m pytest tests/test_app_tui.py -q -k 'stream or markdown_update or selection_state'
```

Expected: 流式节流、异步 update 观察、工具前后分段和选择生命周期测试全部通过；更新中的 widget 及 Block 为不可选，封闭后的每一段均可选。

- [ ] **Step 7：提交独立的输出选择复制切片**

```bash
git add firstcoder/app/tui.py firstcoder/app/tui_view.py firstcoder/app/tui_widgets.py tests/test_app_tui.py
git diff --cached --check
git commit -m "Enable stable TUI output selection"
```

## Task 4：完整回归与真实终端验收

**Files:**

- Verify: `firstcoder/app/tui.py`
- Verify: `firstcoder/app/tui_view.py`
- Verify: `firstcoder/app/tui_widgets.py`
- Verify: `tests/test_app_tui.py`

- [ ] **Step 1：运行完整 TUI 测试文件**

```bash
.venv/bin/python -m pytest tests/test_app_tui.py -q
```

Expected: 全部通过，无选择、粘贴或流式 Markdown 新失败。

- [ ] **Step 2：运行全量测试套件**

```bash
.venv/bin/python -m pytest
```

Expected: 全部通过；必须记录最终 passed 数、warnings 数和退出码，不能只根据进度点判断成功。

- [ ] **Step 3：检查补丁格式和提交边界**

```bash
git diff --check
git status --short
git log -2 --oneline
```

Expected: `git diff --check` 无输出；生产代码和测试没有未提交更改；最近两次提交分别对应粘贴修复和输出选择修复。

- [ ] **Step 4：在真实 Textual TUI 中做交互验收**

```bash
.venv/bin/python -m firstcoder
```

逐项验证并记录结果：

1. 复制普通单行和多行文本后按 `Ctrl+V` 或 `Cmd+V`，输入框各只出现一份内容。
2. 粘贴图片文件路径、系统剪贴板图片以及按 `F8`，附件 chip 各只出现一次，路径不残留在输入框。
3. 鼠标拖选普通段落、列表、代码块和跨 Block 文本；按 `Ctrl+C`/`Cmd+C` 后能粘贴出精确子范围。
4. 没有输出选区且 Composer 没有文本选区时按 `Ctrl+C`，应用仍退出。
5. 在 Composer 选中文字按 `Ctrl+C`/`Cmd+C`，只复制输入文字，不退出应用。
6. 模型仍在流式更新时反复拖选和右键，应用不崩溃；响应完成后该段可以选择复制。
7. 触发一次带工具调用、前后都有文本的响应，工具前后的两段最终都可以选择复制。

若第 6 项仍能触发 Textual selection traceback，保留 traceback 和具体 Markdown 结构，回到 Task 3 收紧生命周期；不要恢复全局 `ALLOW_SELECT=False` 作为回避方案。

- [ ] **Step 5：如真实终端验收暴露测试未覆盖的行为，先补失败测试再修复**

每个新问题单独遵循 Red → 最小实现 → Green，并重新运行：

```bash
.venv/bin/python -m pytest tests/test_app_tui.py -q
.venv/bin/python -m pytest
git diff --check
```

Expected: 自动测试与真实终端七项验收同时通过，才算修复完成。
