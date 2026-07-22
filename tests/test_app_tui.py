import asyncio

import pytest
from rich.text import Text
from textual import events
from textual.widgets import Markdown
from textual.widgets import TextArea

from firstcoder.agent.loop import ToolExecutionEvent
from firstcoder.app.commands import CommandResult
from firstcoder.app.commands import ContextCommandHandler
from firstcoder.runtime.user_input import UserInputOption, UserInputRequest
from firstcoder.app.router import CompositeCommandHandler
from firstcoder.app.runtime import CurrentSessionState
from firstcoder.app.session_commands import SessionCommandHandler
from firstcoder.app.tui import ComposerTextArea, FirstCoderApp, FirstCoderTuiConfig
from firstcoder.app.tui import FirstCoderMarkdown
from firstcoder.app.tui import _entry_renderable
from firstcoder.app.tui import _provider_name_markup
from firstcoder.app.tui import _provider_model_markup
from firstcoder.app.tui import _plain_static
from firstcoder.app.tui import _observe_markdown_update
from firstcoder.app.picker import TuiPickerItem, TuiPickerState, render_picker
from firstcoder.app.picker_adapters import render_picker_item
from firstcoder.app.activity_view import task_plan_panel_text, tool_event_label, tool_event_status, turn_metrics_text
from firstcoder.app.welcome import welcome_renderable
from firstcoder.app.transcript_view import entry_classes, tool_event_entry_kind
from firstcoder.app.tui_state import TuiEntryKind, TuiTaskPlanPanelState, TuiTranscript
from firstcoder.app.tui_state import TuiTranscriptEntry
from firstcoder.context.models import SessionView
from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.agent.session import AgentSession
from firstcoder.input.attachments import UserAttachment
from firstcoder.session.catalog import SessionCatalog
from firstcoder.session.new import NewSessionService
from firstcoder.session.resume import ResumeService
from firstcoder.providers.types import ChatResponse, ChatStreamEvent, TokenUsage, ToolCall
from firstcoder.tools.types import ToolResult
from firstcoder.planning.models import Task, TaskPlan


class FakeOutput:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.mounted: list[object] = []
        self.scroll_end_calls = 0
        self.scroll_y = 0
        self.max_scroll_y = 0

    def write(self, line: str) -> None:
        if self.lines:
            self.lines[-1] += line
        else:
            self.lines.append(line)

    def write_line(self, line: str) -> None:
        self.lines.append(line)

    def mount(self, widget: object) -> None:
        self.mounted.append(widget)
        if type(widget).__name__ == "Static":
            self.lines.append(str(getattr(widget, "content", getattr(widget, "renderable", ""))))
        if isinstance(widget, Markdown):
            widget.updates = []  # type: ignore[attr-defined]

            def update(markdown: str) -> None:
                widget.updates.append(markdown)  # type: ignore[attr-defined]

            widget.update = update  # type: ignore[method-assign]

    def scroll_end(self, animate: bool = False) -> None:
        self.scroll_end_calls += 1
        return None


def _static_output_text(app: FirstCoderApp) -> str:
    static_text = "\n".join(str(getattr(widget, "content", getattr(widget, "renderable", ""))) for widget in app.query_one("#output").query("Static"))
    markdown_text = "\n".join(str(getattr(widget, "source", "") or "\n".join(getattr(widget, "updates", []) or [])) for widget in app.query_one("#output").query("FirstCoderMarkdown"))
    return "\n".join(part for part in [static_text, markdown_text] if part)


class FakeMarkdownUpdateResult:
    def __init__(self, exception: BaseException | None) -> None:
        self._exception = exception
        self.exception_observed = False
        self.callbacks = []
        self._future = self

    def add_done_callback(self, callback) -> None:
        self.callbacks.append(callback)

    def exception(self) -> BaseException | None:
        self.exception_observed = True
        return self._exception

    def finish(self) -> None:
        for callback in self.callbacks:
            callback(self)


class FakeActivity:
    def __init__(self) -> None:
        self.updates: list[str] = []
        self.renderables: list[object] = []
        self.size = type("Size", (), {"width": 60})()

    def update(self, text: object) -> None:
        self.renderables.append(text)
        plain = getattr(text, "plain", None)
        self.updates.append(str(plain if plain is not None else text))


class FakeTopbar(FakeActivity):
    pass


class FakeTaskPlanPanel(FakeActivity):
    def __init__(self) -> None:
        super().__init__()
        self.classes: set[str] = set()

    def add_class(self, name: str) -> None:
        self.classes.add(name)

    def remove_class(self, name: str) -> None:
        self.classes.discard(name)


def test_stage_paste_attachments_adds_clipboard_attachment(monkeypatch, tmp_path) -> None:
    image = tmp_path / "clipboard.png"
    image.write_bytes(b"image")
    attachment = UserAttachment(
        kind="image",
        path=image,
        filename="clipboard.png",
        media_type="image/png",
        size_bytes=image.stat().st_size,
        source="clipboard",
    )
    app = FirstCoderApp()
    messages: list[str] = []
    monkeypatch.setattr("firstcoder.app.tui.resolve_paste_attachments", lambda text: [attachment])
    monkeypatch.setattr(app, "_write_line", lambda text, **kwargs: messages.append(text))

    assert app._stage_paste_attachments(None) is True
    assert app._staged_attachments == [attachment]
    assert messages == ["Attached: 🖼 clipboard.png (5B)"]


def test_stage_paste_attachments_does_not_add_duplicates(monkeypatch, tmp_path) -> None:
    image = tmp_path / "clipboard.png"
    image.write_bytes(b"image")
    attachment = UserAttachment("image", image, "clipboard.png", "image/png", 5, "clipboard")
    app = FirstCoderApp()
    app._staged_attachments.append(attachment)
    monkeypatch.setattr("firstcoder.app.tui.resolve_paste_attachments", lambda text: [attachment])

    assert app._stage_paste_attachments(None) is True
    assert app._staged_attachments == [attachment]


def test_stage_paste_attachments_reports_attachment_errors(monkeypatch) -> None:
    app = FirstCoderApp()
    messages: list[tuple[str, TuiEntryKind]] = []
    monkeypatch.setattr(
        "firstcoder.app.tui.resolve_paste_attachments",
        lambda text: (_ for _ in ()).throw(ValueError("Image exceeds 20MB limit: clipboard.png")),
    )
    monkeypatch.setattr(
        app,
        "_write_line",
        lambda text, *, kind: messages.append((text, kind)),
    )

    assert app._stage_paste_attachments(None) is True
    assert messages == [("Could not attach pasted image: Image exceeds 20MB limit: clipboard.png", TuiEntryKind.ERROR)]


def test_skill_picker_item_renderer_keeps_name_path_and_description_separate() -> None:
    picker = TuiPickerState(kind="skill", title="Select a skill:", items=[])
    item = TuiPickerItem(
        id="skills/very-long.md",
        label="very-long",
        detail=" ".join(["description"] * 30),
        meta={"scope": "global", "path": "skills/very-long.md"},
    )

    rendered = render_picker_item(picker, item, 0)

    assert rendered == "very-long\n    global · skills/very-long.md"


def test_skill_picker_render_keeps_item_heights_stable_and_detail_in_footer() -> None:
    picker = TuiPickerState(
        kind="skill",
        title="Select a skill:",
        items=[
            TuiPickerItem(
                id="skills/brief.md",
                label="brief",
                detail="Write a brief.",
                meta={"scope": "project", "path": "skills/brief.md"},
            ),
            TuiPickerItem(
                id="skills/review.md",
                label="review",
                detail="",
                meta={"scope": "project", "path": "skills/review.md"},
            ),
        ],
        selected_index=0,
    )

    rendered = render_picker(
        picker,
        limit=20,
        render_item=lambda item, index: render_picker_item(picker, item, index),
    )

    assert "Write a brief." not in rendered.splitlines()[1:5]
    assert "Selected: Write a brief." in rendered
    assert "> 1. brief\n    project · skills/brief.md" in rendered
    assert "  2. review\n    project · skills/review.md" in rendered


def test_command_picker_renderable_colors_selected_cursor() -> None:
    entry = TuiTranscriptEntry(
        id=1,
        kind=TuiEntryKind.COMMAND,
        label="command",
        body="",
    )

    rendered = _entry_renderable(entry, "Select:\n> 1. first\n  2. second")

    assert isinstance(rendered, Text)
    assert rendered.plain == "Select:\n> 1. first\n  2. second"
    assert any(span.start == len("Select:\n") and span.end == len("Select:\n>") for span in rendered.spans)
    assert any(span.style == "#7bba55 bold" for span in rendered.spans)


def test_picker_rerender_updates_existing_command_widget_without_full_rerender(monkeypatch) -> None:
    class FakeWidget:
        def __init__(self) -> None:
            self.updates: list[object] = []
            self.classes: str | None = None

        def update(self, renderable) -> None:
            self.updates.append(renderable)

    app = FirstCoderApp()
    widget = FakeWidget()
    entry = app.transcript.add(TuiEntryKind.COMMAND, "Select:\n> 1. first\n  2. second")
    entry.widget = widget
    app._picker = TuiPickerState(
        kind="model",
        title="Select a model:",
        items=[
            TuiPickerItem(id="old", label="old"),
            TuiPickerItem(id="new", label="new"),
        ],
        selected_index=0,
    )
    rerendered = False

    def fail_full_rerender() -> None:
        nonlocal rerendered
        rerendered = True

    monkeypatch.setattr(app, "_rerender_transcript", fail_full_rerender)

    app._picker.move(1)
    app._render_picker()

    assert rerendered is False
    assert len(widget.updates) == 1
    assert getattr(widget.updates[0], "plain", "") == "Select a model:\n  1. old\n> 2. new\nUse up/down and enter to select."
    assert app.transcript.entries[-1].body.startswith("Select a model:")
    assert "> 2. new" in app.transcript.entries[-1].body


class RecordingCommandHandler:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def handle(self, text: str) -> CommandResult:
        self.commands.append(text)
        if text in {"/model", "/models"}:
            return CommandResult(
                handled=True,
                output="Select a model:",
                action={
                    "type": "model_picker",
                    "models": [
                        {"provider": "fake", "model": "old"},
                        {"provider": "fake", "model": "new"},
                    ],
                    "selected_index": 0,
                },
            )
        if text == "/model fake/new":
            return CommandResult(
                handled=True,
                output="Model switched: fake/new",
                action={"type": "model_changed", "provider": "fake", "model": "new"},
            )
        if text == "/skills":
            return CommandResult(
                handled=True,
                output="Skills:",
                action={
                    "type": "skill_picker",
                    "skills": [
                        {
                            "name": "brief",
                            "path": "skills/brief.md",
                            "scope": "project",
                            "description": "Write a brief.",
                        },
                        {
                            "name": "review",
                            "path": "skills/review.md",
                            "scope": "project",
                            "description": "Review work.",
                        },
                    ],
                    "selected_index": 0,
                },
            )
        if text == "/skill-use skills/review.md":
            return CommandResult(
                handled=True,
                output="Referenced skill: review skills/review.md",
                action={
                    "type": "skill_referenced",
                    "name": "review",
                    "path": "skills/review.md",
                    "reference": "请使用 skills/review.md ",
                },
            )
        return CommandResult(handled=False)


@pytest.mark.parametrize(
    ("elapsed_seconds", "expected"),
    [
        (0, "0.0s · 0 tools"),
        (59.9, "59.9s · 0 tools"),
        (60, "1m 0s · 0 tools"),
        (61, "1m 1s · 0 tools"),
        (3599, "59m 59s · 0 tools"),
        (3600, "1h 0m 0s · 0 tools"),
        (3661, "1h 1m 1s · 0 tools"),
    ],
)
def test_turn_metrics_time_units_appear_only_after_thresholds(elapsed_seconds, expected) -> None:
    assert turn_metrics_text(elapsed_seconds, 0) == expected


class FakeSession:
    session_id = "sess_test"
    mode = "standard"
    runtime_state = SessionRuntimeState(session_id="sess_test")

    def rebuild_view(self) -> SessionView:
        return SessionView(session_id="sess_test")


def test_firstcoder_app_can_be_created_with_command_handler() -> None:
    handler = ContextCommandHandler(session=FakeSession())

    app = FirstCoderApp(command_handler=handler, config=FirstCoderTuiConfig(title="TestCoder"))

    assert app.command_handler is handler
    assert app.config.title == "TestCoder"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_uses_custom_chrome_instead_of_textual_header_footer() -> None:
    app = FirstCoderApp(current_session=FakeSession())

    async with app.run_test():
        widget_types = [type(widget).__name__ for widget in app.query("*")]
        widget_ids = [getattr(widget, "id", None) for widget in app.query("*")]

    assert "Header" not in widget_types
    assert "Footer" not in widget_types
    assert "topbar" in widget_ids
    assert "main" in widget_ids


@pytest.mark.parametrize(
    ("mode", "color"),
    [
        ("standard", "#cfd1d6"),
        ("aggressive", "#f6b73c"),
        ("bypass", "#ff6b5f"),
    ],
)
def test_firstcoder_app_topbar_colors_each_permission_mode(mode, color) -> None:
    class ModeSession(FakeSession):
        pass

    session = ModeSession()
    session.mode = mode
    app = FirstCoderApp(current_session=session)

    assert app._topbar_text() == ("[#7bba55]FirstCoder[/]   [#303238]·[/]   [#7bba55]idle · ready[/]   " f"[#303238]·[/]   [{color}]{mode}[/]")
    assert "sess_test" not in app._topbar_text()


def test_firstcoder_app_topbar_shows_a_green_provider_and_hides_session_id() -> None:
    app = FirstCoderApp(
        current_session=FakeSession(),
        config=FirstCoderTuiConfig(
            provider_name="yurenapi",
            provider_model="gpt-5.5",
            project_name="FirstCoder",
        ),
    )

    assert app._topbar_text() == (
        "[#7bba55]FirstCoder[/]   [#303238]·[/]   [#7bba55]idle · ready[/]   "
        "[#303238]·[/]   [#7bba55]yurenapi[/][#6e6d72]/gpt-5.5[/]   "
        "[#303238]·[/]   [#cfd1d6]standard[/]   [#303238]·[/]   [#6e6d72]cwd FirstCoder[/]"
    )


@pytest.mark.parametrize(
    ("model", "colour"),
    [
        ("gpt-5.6-terra", "#18cfcb"),
        ("gpt-5.6-sol", "#ff5c3d"),
        ("gpt-5.6-luna", "#b9c8ff"),
        ("gpt-5.5", "#7eb6ff"),
        ("gpt-5.4", "#57c5f0"),
        ("gpt-5.4-mini", "#9be7c8"),
        ("grok-4.5", "#b57bff"),
        ("fable-5", "#f0a05a"),
        ("opus-4-7", "#ff8f6b"),
        ("opus-4-8", "#ff6f61"),
        ("sonnet-5", "#f0b36a"),
        ("sonnet-4-6", "#f0a18c"),
    ],
)
def test_supported_yuren_models_use_distinct_moving_colour_bands(model: str, colour: str) -> None:
    first = _provider_model_markup("Yuren", model, glow_frame=0)
    next_frame = _provider_model_markup("Yuren", model, glow_frame=1)

    assert Text.from_markup(first).plain == f"Yuren/{model}"
    assert first != next_frame
    assert f"[{colour}]" in first
    assert "[#6e6d72]/[/]" in first


def test_other_provider_names_keep_the_standard_green() -> None:
    assert _provider_name_markup("OpenAI", glow_frame=4) == "[#7bba55]OpenAI[/]"
    assert _provider_model_markup("OpenAI", "gpt-5.6", glow_frame=4) == "[#7bba55]OpenAI[/][#6e6d72]/gpt-5.6[/]"
    # Same model ids only glow under the Yuren provider display name.
    assert _provider_model_markup("yurenapi", "gpt-5.5", glow_frame=0) == ("[#7bba55]yurenapi[/][#6e6d72]/gpt-5.5[/]")
    assert _provider_model_markup("OpenAI", "grok-4.5", glow_frame=0) == ("[#7bba55]OpenAI[/][#6e6d72]/grok-4.5[/]")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_yuren_provider_glow_animates_and_stops_when_the_app_unmounts() -> None:
    app = FirstCoderApp(config=FirstCoderTuiConfig(provider_name="Yuren", provider_model="gpt-5.6-terra"))

    async with app.run_test():
        timer = app._provider_glow_timer
        assert timer is not None
        before = app._topbar_text()
        app._advance_provider_glow()
        after = app._topbar_text()
        assert before != after

    assert timer is not None
    assert app._provider_glow_timer is None


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_unsupported_yuren_model_does_not_start_provider_glow() -> None:
    app = FirstCoderApp(config=FirstCoderTuiConfig(provider_name="Yuren", provider_model="other-model"))

    async with app.run_test():
        assert app._provider_glow_timer is None


def test_observe_markdown_update_consumes_cancelled_update_result() -> None:
    result = FakeMarkdownUpdateResult(asyncio.CancelledError())

    _observe_markdown_update(result)
    result.finish()

    assert result.exception_observed is True


def test_observe_markdown_update_does_not_consume_unexpected_update_errors() -> None:
    result = FakeMarkdownUpdateResult(RuntimeError("markdown failed"))

    _observe_markdown_update(result)
    with pytest.raises(RuntimeError, match="markdown failed"):
        result.finish()


def test_firstcoder_markdown_does_not_enter_textual_selection_path() -> None:
    markdown = FirstCoderMarkdown()

    assert markdown.allow_select is False


def test_firstcoder_markdown_blocks_do_not_enter_textual_selection_path() -> None:
    assert FirstCoderMarkdown.BLOCKS
    assert all(block.ALLOW_SELECT is False for block in FirstCoderMarkdown.BLOCKS.values())


def test_welcome_renderable_uses_colored_full_block_pixels() -> None:
    renderable = welcome_renderable()
    text = renderable.renderable
    next_text = welcome_renderable(particle_frame=1).renderable

    assert renderable.align == "center"
    assert "██" in text.plain
    assert "▀" not in text.plain
    assert "FirstCoder" not in text.plain
    assert "Commands:" not in text.plain
    assert any(span.style == "#81e8bb" for span in text.spans)
    assert any(span.style == "#18cfcb" for span in text.spans)
    assert any(span.style == "#f5fcfa" for span in text.spans)
    assert any(span.style == "#b8ffdf" for span in text.spans)
    assert text.plain != next_text.plain


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_shows_welcome_until_first_input() -> None:
    runner = FakeAsyncChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test(size=(120, 40)) as pilot:
        welcome = app.query_one("#welcome")
        content = welcome.content
        plain = getattr(getattr(content, "renderable", content), "plain", str(content))
        assert "██" in plain
        assert "Commands:" not in plain

        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()

        assert not app.query("#welcome")
        assert app._welcome_particle_timer is None

    assert runner.inputs == ["hello"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_welcome_particles_animate_between_frames() -> None:
    app = FirstCoderApp()

    async with app.run_test(size=(120, 40)):
        welcome = app.query_one("#welcome")
        before = welcome.content
        app._advance_welcome_particles()
        after = welcome.content

    assert before != after


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_uses_compact_welcome_in_an_80_by_24_terminal() -> None:
    app = FirstCoderApp()

    async with app.run_test(size=(80, 24)) as pilot:
        welcome = app.query_one("#welcome")
        plain = getattr(getattr(welcome.content, "renderable", welcome.content), "plain", str(welcome.content))

        assert "firstcoder" in plain
        assert "██" not in plain
        assert app._welcome_particle_timer is None
        assert app.query_one("#input").display is True

        await pilot.resize_terminal(120, 40)
        await pilot.pause(0.2)

        full_welcome = app.query_one("#welcome")
        full_plain = getattr(getattr(full_welcome.content, "renderable", full_welcome.content), "plain", str(full_welcome.content))
        assert "██" in full_plain
        assert app._welcome_particle_timer is not None


def test_firstcoder_app_topbar_uses_spacious_two_sided_layout_when_width_is_known() -> None:
    app = FirstCoderApp(
        current_session=FakeSession(),
        config=FirstCoderTuiConfig(
            provider_name="yurenapi",
            provider_model="gpt-5.5",
            project_name="FirstCoder",
        ),
    )

    text = app._topbar_text(width=120)

    assert text.startswith("[#7bba55]FirstCoder[/]")
    assert "[#7bba55]idle · ready[/]" in text
    assert "sess_test" not in text
    assert "[#7bba55]yurenapi[/][#6e6d72]/gpt-5.5[/]" in text
    assert "[#6e6d72]cwd FirstCoder[/]" in text
    assert " " * 20 in text


def test_firstcoder_app_topbar_highlights_bypass_mode_and_truncates_long_session() -> None:
    class BypassSession(FakeSession):
        session_id = "sess_c8d401e2124f"
        mode = "bypass"

    app = FirstCoderApp(current_session=BypassSession())

    assert app._topbar_text() == ("[#7bba55]FirstCoder[/]   [#303238]·[/]   [#7bba55]idle · ready[/]   " "[#303238]·[/]   [#ff6b5f]bypass[/]")


def test_firstcoder_app_topbar_includes_live_activity_status() -> None:
    app = FirstCoderApp(current_session=FakeSession())

    app._activity_text = "thinking [.. ] planning next step..."

    assert "[#7bba55]thinking [.. ] planning next step...[/]" in app._topbar_text(width=120)


def test_firstcoder_app_topbar_truncates_long_activity_before_metadata() -> None:
    app = FirstCoderApp(
        current_session=FakeSession(),
        config=FirstCoderTuiConfig(
            provider_name="yurenapi",
            provider_model="very-long-model-name",
            project_name="FirstCoder",
        ),
    )
    app._activity_text = "thinking [...] " + "reading think tool result " * 8

    text = app._topbar_text(width=150)

    assert "[#7bba55]yurenapi[/][#6e6d72]/very-long-model-name[/]" in text
    assert "[#6e6d72]cwd FirstCoder[/]" in text
    assert "reading think tool result reading think tool result" not in text
    assert "thinking" in Text.from_markup(text).plain


def test_firstcoder_app_topbar_fits_narrow_width_with_long_activity_and_metadata() -> None:
    app = FirstCoderApp(
        current_session=FakeSession(),
        config=FirstCoderTuiConfig(
            provider_name="yurenapi",
            provider_model="very-long-model-name",
            project_name="FirstCoder",
        ),
    )
    app._activity_text = "thinking [...] " + "reading think tool result " * 8

    text = app._topbar_text(width=80)
    plain = Text.from_markup(text).plain

    assert "\n" not in plain
    assert len(plain) <= 80
    assert "sess_test" not in plain
    assert plain.startswith("FirstCoder")

    narrow_plain = Text.from_markup(app._topbar_text(width=60)).plain

    assert "\n" not in narrow_plain
    assert len(narrow_plain) <= 60
    assert "sess_test" not in narrow_plain
    assert narrow_plain.startswith("FirstCoder")


def test_firstcoder_app_topbar_truncates_narrow_metadata_to_one_row() -> None:
    app = FirstCoderApp(
        current_session=FakeSession(),
        config=FirstCoderTuiConfig(
            provider_name="yurenapi",
            provider_model="very-long-model-name",
            project_name="FirstCoder",
        ),
    )

    plain = Text.from_markup(app._topbar_text(width=60)).plain

    assert "\n" not in plain
    assert len(plain) <= 60
    assert plain.startswith("FirstCoder")
    assert "idle" in plain
    assert "sess_test" not in plain


def test_tui_transcript_records_structured_entries_with_stable_labels() -> None:
    transcript = TuiTranscript()

    user = transcript.add(TuiEntryKind.USER, "hello")
    assistant = transcript.add(TuiEntryKind.ASSISTANT, "hi")
    tool = transcript.add(
        TuiEntryKind.TOOL,
        "rg -n Permission firstcoder",
        label="tool exec_command running",
        status="running",
    )

    assert [entry.id for entry in transcript.entries] == [user.id, assistant.id, tool.id]
    assert [entry.label for entry in transcript.entries] == ["you", "FirstCoder", "tool exec_command running"]
    assert transcript.entries[-1].status == "running"


def test_tui_transcript_tracks_active_tool_until_terminal_status() -> None:
    transcript = TuiTranscript()

    transcript.record_tool_activity("exec_command", "running", "rg -n Permission firstcoder")

    assert transcript.active_tool is not None
    assert transcript.active_tool.name == "exec_command"
    assert transcript.active_tool.status == "running"
    assert transcript.active_tool.summary == "rg -n Permission firstcoder"

    transcript.record_tool_activity("exec_command", "success", "12 matches")

    assert transcript.active_tool is None
    assert transcript.recent_tools[-1].name == "exec_command"
    assert transcript.recent_tools[-1].status == "success"


def test_task_plan_panel_state_keeps_only_last_rendered_revision() -> None:
    state = TuiTaskPlanPanelState()

    assert state.last_rendered_revision is None
    state.last_rendered_revision = 3

    assert state.last_rendered_revision == 3


def test_task_plan_panel_text_renders_linear_tasks_in_ordered_single_column() -> None:
    assert task_plan_panel_text(
        {
            "mode": "linear",
            "revision": 1,
            "ready_task_ids": ["inspect"],
            "blocked_task_ids": ["test"],
            "topological_levels": [["inspect"], ["test"]],
            "tasks": [
                {
                    "id": "test",
                    "content": "跑测试",
                    "status": "pending",
                    "depends_on": [],
                    "order": 20,
                },
                {
                    "id": "inspect",
                    "content": "读代码",
                    "status": "completed",
                    "depends_on": [],
                    "order": 10,
                },
                {
                    "id": "implement",
                    "content": "实现改动",
                    "status": "in_progress",
                    "depends_on": [],
                    "order": 15,
                },
            ],
        }
    ) == ("Task Plan · linear\n" "[✓] 读代码\n" "[~] 实现改动\n" "[!] 跑测试")


def test_task_plan_panel_text_renders_dag_levels_dependencies_and_derived_statuses() -> None:
    assert task_plan_panel_text(
        {
            "mode": "dag",
            "revision": 2,
            "ready_task_ids": ["research_a"],
            "blocked_task_ids": ["summary"],
            "topological_levels": [["research_a", "research_b"], ["summary"]],
            "tasks": [
                {
                    "id": "summary",
                    "content": "汇总",
                    "status": "pending",
                    "depends_on": ["research_a", "research_b"],
                    "order": 30,
                },
                {
                    "id": "research_b",
                    "content": "调研 B",
                    "status": "in_progress",
                    "depends_on": [],
                    "order": 20,
                },
                {
                    "id": "research_a",
                    "content": "调研 A",
                    "status": "pending",
                    "depends_on": [],
                    "order": 10,
                },
            ],
        }
    ) == ("Task Plan · dag\n" "Level 0 · parallel\n" "  [→] 调研 A (research_a)\n" "  [~] 调研 B (research_b)\n" "Level 1\n" "  [!] 汇总 (summary) · depends on: research_a, research_b")


def test_firstcoder_app_records_rendered_messages_in_transcript(monkeypatch) -> None:
    output = FakeOutput()
    app = FirstCoderApp()
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    app._write_line("> hello", kind=TuiEntryKind.USER)
    app._write_markdown_message("**hi**")

    assert [(entry.kind, entry.label, entry.body) for entry in app.transcript.entries] == [
        (TuiEntryKind.USER, "you", "> hello"),
        (TuiEntryKind.ASSISTANT, "FirstCoder", "**hi**"),
    ]


class FakeChatRunner:
    def __init__(self) -> None:
        self.inputs = []
        self.attachments: list[list[UserAttachment] | None] = []
        self.last_display_lines = []

    def run_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> ChatResponse:
        self.inputs.append(content)
        self.attachments.append(attachments)
        return ChatResponse(provider="fake", model="fake", content=f"reply:{content}")


class FakeDisplayChatRunner(FakeChatRunner):
    def run_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        self.last_display_lines = ["Tool call: echo {}", "Tool result: echo success: ok", "done"]
        return ChatResponse(provider="fake", model="fake", content="done")


class FakeAsyncChatRunner(FakeChatRunner):
    async def arun_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> ChatResponse:
        self.inputs.append(content)
        self.attachments.append(attachments)
        self.last_display_lines = ["async reply"]
        return ChatResponse(provider="fake", model="fake", content="async reply")


class FailingAsyncChatRunner(FakeChatRunner):
    async def arun_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        raise RuntimeError("provider down")


class FakeStreamingAsyncChatRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.seen = []
        self.stream_event_handler = self.seen.append

    async def arun_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        self.stream_event_handler(ChatStreamEvent(kind="reasoning_delta", text="thinking"))
        self.last_display_lines = ["done"]
        return ChatResponse(provider="fake", model="fake", content="done")


class FakeStreamingTextAsyncChatRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.stream_event_handler = lambda event: None

    async def arun_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        self.stream_event_handler(ChatStreamEvent(kind="text_delta", text="he"))
        self.stream_event_handler(ChatStreamEvent(kind="text_delta", text="llo"))
        self.last_display_lines = ["hello"]
        return ChatResponse(provider="fake", model="fake", content="hello")


class FakeToolEventAsyncChatRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.tool_event_handler = lambda event: None

    async def arun_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        tool_call = ToolCall(id="call_echo", name="echo", arguments={"text": "hello"})
        self.tool_event_handler(ToolExecutionEvent(kind="started", tool_call=tool_call))
        self.tool_event_handler(
            ToolExecutionEvent(
                kind="finished",
                tool_call=tool_call,
                result=ToolResult(name="echo", ok=True, content="hello"),
            )
        )
        self.last_display_lines = [
            'Tool call: echo {"text": "hello"}',
            "Tool result: echo success: hello",
            "done",
        ]
        return ChatResponse(provider="fake", model="fake", content="done")


class FakePermissionResumeRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.last_pending_input = UserInputRequest(
            id="perm_write",
            kind="permission_confirmation",
            question="允许写 README 吗？",
            options=[
                UserInputOption(id="deny", label="Deny"),
                UserInputOption(id="allow_once", label="Allow once"),
            ],
        )
        self.resumes: list[tuple[str, str]] = []

    async def aresume_with_user_input(self, request_id: str, answer: str) -> ChatResponse:
        self.resumes.append((request_id, answer))
        self.last_pending_input = None
        self.last_display_lines = ["Tool result: write success: ok", "done"]
        return ChatResponse(provider="fake", model="fake", content="done")


class FakePermissionMidTurnRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.last_pending_input = None
        self.resumes: list[tuple[str, str]] = []

    async def arun_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        self.last_pending_input = UserInputRequest(
            id="perm_write",
            kind="permission_confirmation",
            question="允许写 README 吗？",
            options=[
                UserInputOption(id="deny", label="Deny"),
                UserInputOption(id="allow_once", label="Allow once"),
            ],
        )
        return ChatResponse(provider="fake", model="fake", content="等待权限确认。")

    async def aresume_with_user_input(self, request_id: str, answer: str) -> ChatResponse:
        self.resumes.append((request_id, answer))
        self.last_pending_input = None
        self.last_display_lines = ["done"]
        return ChatResponse(provider="fake", model="fake", content="done")


class FakePermissionWaitingRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.last_pending_input = UserInputRequest(
            id="perm_write",
            kind="permission_confirmation",
            question="允许写 README 吗？",
            options=[
                UserInputOption(id="deny", label="Deny"),
                UserInputOption(id="allow_once", label="Allow once"),
                UserInputOption(id="allow_always_same_scope", label="Allow always"),
            ],
            payload={
                "action": "write_path",
                "target": "README.md",
                "reason": "写入文件需要用户确认。",
                "prewrite_review": {
                    "tool_name": "edit",
                    "files": [
                        {
                            "path": "README.md",
                            "operation": "modify",
                            "diff": "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new",
                            "added_lines": 1,
                            "removed_lines": 1,
                        }
                    ],
                    "summary": {"added_lines": 1, "removed_lines": 1},
                    "error": None,
                },
            },
        )


class BlockingAsyncChatRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        import anyio

        self.started = anyio.Event()
        self.release = anyio.Event()

    async def arun_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        self.started.set()
        await self.release.wait()
        return ChatResponse(provider="fake", model="fake", content="done")


class BlockingGuidanceAsyncChatRunner(BlockingAsyncChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.guidance: list[str] = []

    def add_guidance(self, content: str) -> None:
        self.guidance.append(content)


class UnhandledCommandHandler:
    def handle(self, text: str) -> CommandResult:
        return CommandResult(handled=False)


class SubmitChatCommandHandler:
    def handle(self, text: str) -> CommandResult:
        return CommandResult(
            handled=True,
            output="Using skill: brief",
            action={"type": "submit_chat", "text": "请使用 skills/brief.md 写日报"},
        )


def test_firstcoder_app_can_be_created_with_composite_handler_and_chat_runner() -> None:
    context_handler = ContextCommandHandler(session=FakeSession())
    composite = CompositeCommandHandler(
        [
            SessionCommandHandler(catalog=object()),  # constructor storage only; not used by this test
            context_handler,
        ]
    )
    runner = FakeChatRunner()

    app = FirstCoderApp(command_handler=composite, chat_runner=runner)

    assert app.command_handler is composite
    assert app.chat_runner is runner


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_runs_plain_chat_when_only_chat_runner_is_configured() -> None:
    runner = FakeChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")

    assert runner.inputs == ["hello"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_sends_staged_paste_attachment_and_clears_it(tmp_path, monkeypatch) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    attachment = UserAttachment(
        kind="image",
        path=image,
        filename="image.png",
        media_type="image/png",
        size_bytes=image.stat().st_size,
        source="paste",
    )
    monkeypatch.setattr("firstcoder.app.tui.resolve_paste_attachments", lambda text: [attachment])
    runner = FakeChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        app._staged_attachments = [attachment]
        await pilot.press(*"describe")
        await pilot.press("enter")

    assert runner.inputs == ["describe"]
    assert runner.attachments == [[attachment]]
    assert app._staged_attachments == []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_pasting_a_file_path_stages_attachment_without_inserting_path(tmp_path) -> None:
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"video")
    runner = FakeChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        input_widget = app.query_one("#input", TextArea)
        await input_widget._on_paste(events.Paste(str(video)))
        await pilot.pause()

        assert input_widget.text == ""
        assert app._staged_attachments[0].path == video
        await pilot.click("#input")
        await pilot.press("enter")

    assert runner.inputs == ["请分析这些附件。"]
    assert runner.attachments[0][0].path == video


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_pasting_plain_text_keeps_text_in_composer(monkeypatch) -> None:
    monkeypatch.setattr("firstcoder.app.tui.resolve_paste_attachments", lambda text: [])
    app = FirstCoderApp()

    async with app.run_test():
        input_widget = app.query_one("#input", TextArea)
        await input_widget._on_paste(events.Paste("explain this"))
        assert input_widget.text == "explain this"

    assert app._staged_attachments == []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_composer_advertises_ctrl_or_cmd_v_for_image_paste() -> None:
    app = FirstCoderApp()

    async with app.run_test():
        composer = app.query_one("#input", ComposerTextArea)
        assert composer.placeholder == "输入消息，Enter 发送，Shift+Enter 换行，Ctrl/Cmd+V 粘贴图片"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("paste_key", ["ctrl+v", "super+v", "f8"])
async def test_firstcoder_app_paste_shortcut_stages_clipboard_image_while_composer_is_focused(tmp_path, monkeypatch, paste_key) -> None:
    image = tmp_path / "clipboard.png"
    image.write_bytes(b"image")
    attachment = UserAttachment("image", image, "clipboard.png", "image/png", 5, "clipboard")
    monkeypatch.setattr("firstcoder.app.tui.resolve_paste_attachments", lambda text: [attachment])
    app = FirstCoderApp()

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(paste_key)
        input_widget = app.query_one("#input", ComposerTextArea)

    assert input_widget.text == ""
    assert app._staged_attachments == [attachment]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("paste_key", ["ctrl+v", "super+v", "f8"])
async def test_firstcoder_app_paste_shortcut_reports_missing_clipboard_image_while_composer_is_focused(monkeypatch, paste_key) -> None:
    monkeypatch.setattr("firstcoder.app.tui.resolve_paste_attachments", lambda text: [])
    app = FirstCoderApp()

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(paste_key)
        await pilot.pause()

        assert "No clipboard image found" in _static_output_text(app)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_submits_multiline_composer_text() -> None:
    runner = FakeChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        input_widget = app.query_one("#input", TextArea)
        input_widget.load_text("第一句\n第二句\n第三句")
        await pilot.click("#input")
        await pilot.press("enter")

    assert runner.inputs == ["第一句\n第二句\n第三句"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_shift_enter_inserts_newline_without_submitting() -> None:
    runner = FakeChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("shift+enter")
        await pilot.press(*"world")
        input_widget = app.query_one("#input", TextArea)

    assert input_widget.text == "hello\nworld"
    assert runner.inputs == []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_does_not_send_unhandled_slash_command_to_chat_runner() -> None:
    runner = FakeChatRunner()
    app = FirstCoderApp(command_handler=UnhandledCommandHandler(), chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/unknown")
        await pilot.press("enter")

    assert runner.inputs == []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_submits_chat_from_command_action() -> None:
    runner = FakeChatRunner()
    app = FirstCoderApp(command_handler=SubmitChatCommandHandler(), chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/brief 写日报")
        await pilot.press("enter")

    assert runner.inputs == ["请使用 skills/brief.md 写日报"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_displays_session_id_and_runner_display_lines() -> None:
    runner = FakeDisplayChatRunner()
    app = FirstCoderApp(chat_runner=runner, current_session=FakeSession())

    async with app.run_test() as pilot:
        assert app.sub_title == "Session: sess_test"
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")

    assert runner.inputs == ["hello"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_awaits_async_chat_runner_when_available() -> None:
    runner = FakeAsyncChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")

    assert runner.inputs == ["hello"]
    assert runner.last_display_lines == ["async reply"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_queues_guidance_while_chat_is_running() -> None:
    runner = BlockingGuidanceAsyncChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"start")
        await pilot.press("enter")
        await runner.started.wait()
        await pilot.press(*"先别总结")
        await pilot.press("enter")
        await pilot.pause()
        runner.release.set()

    assert runner.inputs == ["start"]
    assert runner.guidance == ["先别总结"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_resume_picker_replays_selected_session_history(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer_one = SessionEventWriter(store=store, session_id="sess_one")
    writer_one.append_session_created(title="第一个")
    writer_one.append_user_message("旧问题")
    writer_one.append_assistant_response(ChatResponse(provider="fake", model="fake", content="旧回答"))
    writer_two = SessionEventWriter(store=store, session_id="sess_two")
    writer_two.append_session_created(title="第二个")
    writer_two.append_user_message("新问题")
    current = AgentSession.resume(store=store, session_id="sess_one", agents_md="")
    state = CurrentSessionState(current)
    handler = SessionCommandHandler(
        catalog=SessionCatalog(tmp_path),
        current_session=state.session,
        resume_service=ResumeService(store=store, project_root=tmp_path),
        on_resume=state.set_session,
    )
    app = FirstCoderApp(command_handler=handler, current_session=state)
    markdown_rendered = False

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/resume")
        await pilot.press("enter")
        await pilot.pause()
        output_text = _static_output_text(app)
        assert "Select a session" in output_text
        assert "第二个" in output_text
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        output_text = _static_output_text(app)
        markdown_rendered = bool(app.query_one("#output").query("FirstCoderMarkdown"))

    assert state.session.session_id == "sess_one"
    assert "旧问题" in output_text
    assert any(entry.body == "旧回答" for entry in app.transcript.entries)
    assert markdown_rendered
    assert "Select a session" not in output_text


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_resume_picker_renders_twenty_visible_rows_and_scrolls(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    for index in range(25):
        writer = SessionEventWriter(store=store, session_id=f"sess_{index:02d}")
        writer.append_session_created(title=f"标题{index:02d}")
        writer.append_user_message(f"问题{index:02d}")
    current = AgentSession.resume(store=store, session_id="sess_00", agents_md="")
    state = CurrentSessionState(current)
    handler = SessionCommandHandler(
        catalog=SessionCatalog(tmp_path),
        current_session=state.session,
        resume_service=ResumeService(store=store, project_root=tmp_path),
        on_resume=state.set_session,
    )
    app = FirstCoderApp(command_handler=handler, current_session=state)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/resume")
        await pilot.press("enter")
        await pilot.pause()
        output_text = _static_output_text(app)
        assert "Showing 1-20 of 25 sessions" in output_text
        assert "sess_24" in output_text
        assert "sess_05" in output_text
        assert "sess_04" not in output_text

        for _ in range(20):
            await pilot.press("down")
        await pilot.pause()
        output_text = _static_output_text(app)
        assert "Showing 2-21 of 25 sessions" in output_text
        assert "sess_24" not in output_text
        assert "sess_04" in output_text
        assert "> 21. sess_04" in output_text
        await pilot.press("enter")
        await pilot.pause()

    assert state.session.session_id == "sess_04"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_model_picker_switches_selected_model() -> None:
    handler = RecordingCommandHandler()
    app = FirstCoderApp(
        command_handler=handler,
        config=FirstCoderTuiConfig(provider_name="fake", provider_model="old"),
    )

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/model")
        await pilot.press("enter")
        await pilot.pause()
        output_text = _static_output_text(app)
        assert "Select a model:" in output_text
        assert "> 1. fake/old" in output_text

        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

    assert handler.commands == ["/model", "/model fake/new"]
    assert app.config.provider_name == "fake"
    assert app.config.provider_model == "new"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_models_alias_opens_model_picker() -> None:
    handler = RecordingCommandHandler()
    app = FirstCoderApp(
        command_handler=handler,
        config=FirstCoderTuiConfig(provider_name="fake", provider_model="old"),
    )

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/models")
        await pilot.press("enter")
        await pilot.pause()
        output_text = _static_output_text(app)

    assert handler.commands == ["/models"]
    assert "Select a model:" in output_text


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_skill_picker_references_selected_skill_in_input() -> None:
    handler = RecordingCommandHandler()
    app = FirstCoderApp(command_handler=handler)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/skills")
        await pilot.press("enter")
        await pilot.pause()
        output_text = _static_output_text(app)
        assert "Select a skill:" in output_text
        assert "> 1. brief\n    project · skills/brief.md" in output_text
        assert "Selected: Write a brief." in output_text

        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        input_widget = app.query_one("#input")

    assert handler.commands == ["/skills", "/skill-use skills/review.md"]
    assert input_widget.text == "请使用 skills/review.md "


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_new_command_clears_previous_output(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_old")
    writer.append_session_created(title="旧会话")
    writer.append_user_message("旧问题")
    current = AgentSession.resume(store=store, session_id="sess_old", agents_md="")
    state = CurrentSessionState(current)
    handler = SessionCommandHandler(
        catalog=SessionCatalog(tmp_path),
        current_session=state.session,
        new_service=NewSessionService(store=store, project_root=tmp_path),
        on_resume=state.set_session,
    )
    app = FirstCoderApp(command_handler=handler, current_session=state)

    async with app.run_test() as pilot:
        app._write_line("> 旧问题", kind=TuiEntryKind.USER)
        app._write_markdown_message("旧回答")
        await pilot.click("#input")
        await pilot.press(*"/new 新会话")
        await pilot.press("enter")
        await pilot.pause()
        output_text = _static_output_text(app)

    assert state.session.session_id != "sess_old"
    assert "New session:" in output_text
    assert "新会话" in output_text
    assert "旧问题" not in output_text
    assert "旧回答" not in output_text
    assert [entry.kind for entry in app.transcript.entries] == [TuiEntryKind.COMMAND]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_double_escape_interrupts_running_chat() -> None:
    runner = BlockingAsyncChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"start")
        await pilot.press("enter")
        await runner.started.wait()
        await pilot.press("escape")
        assert app._chat_busy is True
        await pilot.press("escape")
        await pilot.pause()
        output_text = "\n".join(str(getattr(widget, "content", getattr(widget, "renderable", ""))) for widget in app.query_one("#output").query("Static"))

    assert runner.inputs == ["start"]
    assert app._chat_busy is False
    assert app._activity_text == "interrupted"
    assert "Interrupted current turn." in output_text


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_streaming_tui_does_not_render_duplicate_final_markdown() -> None:
    runner = FakeStreamingTextAsyncChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()
        output = app.query_one("#output")
        markdown_widgets = output.query("Markdown")
        assert len(markdown_widgets) == 1
    assert runner.inputs == ["hello"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_right_clicking_markdown_output_does_not_crash_selection_path() -> None:
    runner = FakeChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()
        markdown = app.query_one("FirstCoderMarkdown")
        assert markdown.allow_select is False
        await pilot.click(markdown, button=3)
        await pilot.pause()

    assert runner.inputs == ["hello"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_right_clicking_markdown_code_block_does_not_crash_selection_path() -> None:
    app = FirstCoderApp()

    async with app.run_test() as pilot:
        app._write_markdown_message("```text\nIt was the best of times\n```")
        assert app.ALLOW_SELECT is False
        await pilot.click("FirstCoderMarkdown", button=3)
        await pilot.pause()


def test_firstcoder_app_installs_and_restores_stream_event_handler() -> None:
    runner = FakeStreamingAsyncChatRunner()
    original_handler = runner.stream_event_handler
    app = FirstCoderApp(chat_runner=runner)

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="message_started"))
    app._restore_stream_event_handler(previous_handler)

    assert runner.seen == [ChatStreamEvent(kind="message_started")]
    assert runner.stream_event_handler is original_handler


def test_firstcoder_app_streams_text_delta_without_repeating_final_text(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    runner.last_display_lines = ["hello"]
    output = FakeOutput()
    app = FirstCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="he"))
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="llo"))
    app._write_chat_response(ChatResponse(provider="fake", model="fake", content="hello"))
    app._restore_stream_event_handler(previous_handler)

    assert [type(widget).__name__ for widget in output.mounted] == ["FirstCoderMarkdown"]
    assert output.mounted[0].allow_select is False
    assert output.mounted[0].updates[-1] == "FirstCoder:\n\nhello"
    assert app._stream_text_buffer == "hello"
    assert runner.seen == [
        ChatStreamEvent(kind="text_delta", text="he"),
        ChatStreamEvent(kind="text_delta", text="llo"),
    ]


def test_firstcoder_app_streaming_skips_normalized_duplicate_assistant_line(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    runner.last_display_lines = ["hello\n"]
    output = FakeOutput()
    app = FirstCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_handler = app._install_stream_event_handler()
    app._start_turn_metrics()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="hello"))
    app._write_chat_response(ChatResponse(provider="fake", model="fake", content="hello"))
    app._restore_stream_event_handler(previous_handler)

    assert [type(widget).__name__ for widget in output.mounted] == ["FirstCoderMarkdown"]
    assert output.mounted[0].allow_select is False
    assert output.mounted[0].updates[-1] == "FirstCoder:\n\nhello"


def test_firstcoder_app_streaming_skips_replaying_intermediate_assistant_lines(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    runner.last_display_lines = [
        "先看详情：",
        'Tool call: shell {"command": "pytest"}',
        "Tool result: shell success: ok",
        "问题找到了：",
        "最终结论",
    ]
    output = FakeOutput()
    app = FirstCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="最终"))
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="结论"))
    app._live_tool_events_seen = True
    app._write_chat_response(ChatResponse(provider="fake", model="fake", content="最终结论"))
    app._restore_stream_event_handler(previous_handler)

    assert [type(widget).__name__ for widget in output.mounted] == ["FirstCoderMarkdown"]
    assert output.mounted[0].updates[-1] == "FirstCoder:\n\n最终结论"
    assert [entry.body for entry in app.transcript.entries if entry.kind == TuiEntryKind.ASSISTANT] == ["最终结论"]


def test_firstcoder_app_paces_stream_markdown_updates(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    output = FakeOutput()
    app = FirstCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)
    monkeypatch.setattr(app, "set_timer", lambda *args, **kwargs: object())

    app._append_stream_text("我")
    app._append_stream_text("在")
    app._append_stream_text("这里")

    markdown = output.mounted[0]
    assert type(markdown).__name__ == "FirstCoderMarkdown"
    assert markdown.allow_select is False
    assert markdown.updates == ["FirstCoder:\n\n我"]
    assert app._stream_text_buffer == "我在这里"

    app._flush_stream_text()

    assert markdown.updates[-1] == "FirstCoder:\n\n我在这里"


def test_firstcoder_app_does_not_scroll_stream_when_render_is_deferred(monkeypatch) -> None:
    output = FakeOutput()
    app = FirstCoderApp()
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_timer", lambda *args, **kwargs: object())

    app._append_stream_text("我")
    after_initial_render = output.scroll_end_calls
    app._append_stream_text("在")
    app._append_stream_text("这里")

    assert after_initial_render == 1
    assert output.scroll_end_calls == after_initial_render


def test_firstcoder_app_does_not_auto_scroll_stream_when_user_is_reading_history(monkeypatch) -> None:
    output = FakeOutput()
    output.scroll_y = 1
    output.max_scroll_y = 10
    app = FirstCoderApp()
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    app._append_stream_text("hello")
    app._flush_stream_text()

    assert output.scroll_end_calls == 0


def test_firstcoder_app_records_streaming_assistant_text_in_transcript(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    output = FakeOutput()
    app = FirstCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    app._append_stream_text("你")
    app._append_stream_text("好")

    assistant_entries = [entry for entry in app.transcript.entries if entry.kind == TuiEntryKind.ASSISTANT]
    assert len(assistant_entries) == 1
    assert assistant_entries[0].body == "你好"


def test_firstcoder_app_shows_reasoning_delta_in_activity_line(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    output = FakeOutput()
    activity = FakeActivity()
    app = FirstCoderApp(chat_runner=runner)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)

    app._append_reasoning_text("planning ")
    app._append_reasoning_text("tools")

    reasoning_entries = [entry for entry in app.transcript.entries if entry.kind == TuiEntryKind.REASONING]
    assert reasoning_entries == []
    assert output.mounted == []
    assert activity.updates[0].startswith("thinking [.  ] planning ")
    assert activity.updates[0].rstrip().endswith("0.0s · 0 tools")
    assert activity.updates[1].startswith("thinking [.  ] planning tools")
    assert activity.updates[1].rstrip().endswith("0.0s · 0 tools")


def test_firstcoder_app_shows_working_indicator_without_reasoning_delta(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    app = FirstCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)

    app._show_working_indicator("planning next step...")
    app._complete_working_indicator()

    reasoning_entries = [entry for entry in app.transcript.entries if entry.kind == TuiEntryKind.REASONING]
    assert reasoning_entries == []
    assert output.mounted == []
    assert activity.updates[0].startswith("thinking [.  ] planning next step...")
    assert activity.updates[0].rstrip().endswith("0.0s · 0 tools")
    assert activity.updates[1].startswith("streaming [>   ] · response")
    assert activity.updates[1].rstrip().endswith("0.0s · 0 tools")


def test_firstcoder_app_animates_working_indicator(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    timer = type("FakeTimer", (), {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)})()
    app = FirstCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    app._show_working_indicator("planning next step...")
    app._advance_working_animation()

    reasoning_entries = [entry for entry in app.transcript.entries if entry.kind == TuiEntryKind.REASONING]
    assert reasoning_entries == []
    assert activity.updates[-1].startswith("thinking [.. ] planning next step...")
    assert activity.updates[-1].rstrip().endswith("0.0s · 0 tools")
    assert app._working_timer is timer

    app._complete_working_indicator()

    assert activity.updates[-1].startswith("streaming [>   ] · response")
    assert activity.updates[-1].rstrip().endswith("0.0s · 0 tools")
    assert timer.stopped is True
    assert app._working_timer is None


def test_firstcoder_app_streaming_final_response_skips_assistant_display_line(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    runner.last_display_lines = ["hello", "Tool result: echo success: ok"]
    output = FakeOutput()
    app = FirstCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="h"))
    app._write_chat_response(ChatResponse(provider="fake", model="fake", content="hello"))
    app._restore_stream_event_handler(previous_handler)

    assert sum(isinstance(widget, Markdown) for widget in output.mounted) == 1
    assert [type(widget).__name__ for widget in output.mounted] == ["FirstCoderMarkdown", "Static"]
    assert output.mounted[0].allow_select is False


def test_firstcoder_app_replaces_partial_stream_when_final_response_differs(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    runner.last_display_lines = ["complete ok"]
    output = FakeOutput()
    app = FirstCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="partial"))
    app._write_chat_response(ChatResponse(provider="fake", model="fake", content="complete ok"))
    app._restore_stream_event_handler(previous_handler)

    assert [type(widget).__name__ for widget in output.mounted] == ["FirstCoderMarkdown"]
    assert output.mounted[0].updates[-1] == "FirstCoder:\n\ncomplete ok"
    assert app._stream_text_buffer == "complete ok"


def test_firstcoder_app_stops_streaming_status_after_final_response(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    output = FakeOutput()
    activity = FakeActivity()
    timer = type("FakeTimer", (), {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)})()
    app = FirstCoderApp(chat_runner=runner)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="hello"))
    app._write_chat_response(
        ChatResponse(
            provider="fake",
            model="fake",
            content="hello",
            usage=TokenUsage(input_tokens=3, output_tokens=5, total_tokens=8),
        )
    )
    app._restore_stream_event_handler(previous_handler)

    assert activity.updates[-1].startswith("done")
    assert activity.updates[-1].rstrip().endswith("0.0s · 0 tools")
    assert "tok" not in activity.updates[-1]
    assert timer.stopped is True
    assert app._activity_timer is None


def test_firstcoder_app_stream_event_handler_schedules_ui_updates_on_app_thread(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    app = FirstCoderApp(chat_runner=runner)
    calls: list[tuple[str, str]] = []
    scheduled: list[object] = []

    monkeypatch.setattr(app, "_append_stream_text", lambda text: calls.append(("text", text)))
    monkeypatch.setattr(app, "_call_ui_thread", lambda callback, *args, **kwargs: scheduled.append((callback, args, kwargs)))

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="hello"))
    app._restore_stream_event_handler(previous_handler)

    assert calls == []
    assert len(scheduled) == 2
    callback, args, kwargs = scheduled[1]
    callback(*args, **kwargs)
    assert calls == [("text", "hello")]


def test_firstcoder_app_tool_event_handler_schedules_live_status_on_app_thread(monkeypatch) -> None:
    runner = FakeToolEventAsyncChatRunner()
    app = FirstCoderApp(chat_runner=runner)
    calls: list[str] = []
    scheduled: list[object] = []

    monkeypatch.setattr(app, "_write_line", lambda text, **kwargs: calls.append(text))
    monkeypatch.setattr(app, "_call_ui_thread", lambda callback, *args, **kwargs: scheduled.append((callback, args, kwargs)))

    previous_handler = app._install_tool_event_handler()
    runner.tool_event_handler(ToolExecutionEvent(kind="started", tool_call=ToolCall(id="call_echo", name="echo", arguments={})))
    app._restore_tool_event_handler(previous_handler)

    assert calls == []
    assert len(scheduled) == 3
    callback, args, kwargs = scheduled[2]
    callback(*args, **kwargs)
    assert calls == ["正在调用工具：echo"]


def test_firstcoder_app_updates_activity_line_for_tool_events(monkeypatch) -> None:
    runner = FakeToolEventAsyncChatRunner()
    output = FakeOutput()
    activity = FakeActivity()
    timer = type("FakeTimer", (), {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)})()
    app = FirstCoderApp(chat_runner=runner)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    previous_handler = app._install_tool_event_handler()
    tool_call = ToolCall(id="call_echo", name="echo", arguments={"text": "hello"})
    runner.tool_event_handler(ToolExecutionEvent(kind="started", tool_call=tool_call))
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="finished",
            tool_call=tool_call,
            result=ToolResult(name="echo", ok=True, content="hello"),
        )
    )
    app._restore_tool_event_handler(previous_handler)

    assert activity.updates[0].startswith("running [=   ] · echo")
    assert activity.updates[0].rstrip().endswith("0.0s · 1 tool")
    assert activity.updates[1].startswith("thinking [.  ] reading echo result")
    assert activity.updates[1].rstrip().endswith("0.0s · 1 tool")
    assert app._working_timer is timer

    app._advance_working_animation()

    assert activity.updates[-1].startswith("thinking [.. ] reading echo result")
    assert activity.updates[-1].rstrip().endswith("0.0s · 1 tool")


def test_firstcoder_app_stops_post_tool_animation_when_next_tool_starts(monkeypatch) -> None:
    runner = FakeToolEventAsyncChatRunner()
    output = FakeOutput()
    activity = FakeActivity()
    timer = type("FakeTimer", (), {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)})()
    app = FirstCoderApp(chat_runner=runner)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    previous_handler = app._install_tool_event_handler()
    first_call = ToolCall(id="call_echo", name="echo", arguments={})
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="finished",
            tool_call=first_call,
            result=ToolResult(name="echo", ok=True, content="hello"),
        )
    )
    runner.tool_event_handler(ToolExecutionEvent(kind="started", tool_call=ToolCall(id="call_ls", name="ls", arguments={})))
    app._restore_tool_event_handler(previous_handler)

    assert timer.stopped is True
    assert app._working_timer is None
    assert activity.updates[-1].startswith("running [=   ] · ls")
    assert activity.updates[-1].rstrip().endswith("0.0s · 1 tool")


def test_firstcoder_app_activity_line_summarizes_parallel_tool_events(monkeypatch) -> None:
    runner = FakeToolEventAsyncChatRunner()
    output = FakeOutput()
    activity = FakeActivity()
    timer = type("FakeTimer", (), {"stop": lambda self: None})()
    app = FirstCoderApp(chat_runner=runner)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    previous_handler = app._install_tool_event_handler()
    runner.tool_event_handler(ToolExecutionEvent(kind="started", tool_call=ToolCall(id="call_view_1", name="view", arguments={})))
    runner.tool_event_handler(ToolExecutionEvent(kind="started", tool_call=ToolCall(id="call_view_2", name="view", arguments={})))
    app._restore_tool_event_handler(previous_handler)

    assert activity.updates[-1].startswith("running [=   ] · 2 tools running")
    assert activity.updates[-1].rstrip().endswith("0.0s · 2 tools")


def test_firstcoder_app_animates_running_tool_status(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    timer = type("FakeTimer", (), {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)})()
    app = FirstCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    app._start_turn_metrics()
    app._show_activity_animation("running", "echo")
    app._advance_activity_animation()

    assert activity.updates[0].startswith("running [=   ] · echo")
    assert activity.updates[0].rstrip().endswith("0.0s · 0 tools")
    assert activity.updates[1].startswith("running [==  ] · echo")
    assert activity.updates[1].rstrip().endswith("0.0s · 0 tools")
    app._advance_activity_animation()
    assert activity.updates[-1].startswith("running [=== ] · echo")
    assert activity.updates[-1].rstrip().endswith("0.0s · 0 tools")
    assert app._activity_timer is timer

    app._stop_activity_animation()

    assert timer.stopped is True
    assert app._activity_timer is None


def test_firstcoder_app_keeps_elapsed_time_live_after_tool_failure(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    timer = type("FakeTimer", (), {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)})()
    app = FirstCoderApp()
    clock = {"now": 100.0}

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)
    monkeypatch.setattr("firstcoder.app.tui.time.monotonic", lambda: clock["now"])

    tool_call = ToolCall(id="call_echo", name="echo", arguments={})
    app._start_turn_metrics()
    app._record_tool_activity(ToolExecutionEvent(kind="started", tool_call=tool_call))
    app._record_tool_activity(
        ToolExecutionEvent(
            kind="finished",
            tool_call=tool_call,
            result=ToolResult(name="echo", ok=False, content="boom"),
        )
    )

    assert activity.updates[-1].startswith("error · echo")
    assert activity.updates[-1].rstrip().endswith("0.0s · 1 tool")
    assert app._activity_timer is timer

    clock["now"] = 102.5
    app._advance_activity_animation()

    assert activity.updates[-1].startswith("error · echo")
    assert activity.updates[-1].rstrip().endswith("2.5s · 1 tool")


def test_firstcoder_app_activity_line_uses_plain_status_text(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    app = FirstCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)

    app._set_activity("running [=   ] · echo")

    renderable = activity.renderables[-1]
    assert getattr(renderable, "plain", "").startswith("running [=   ] · echo")
    assert getattr(renderable, "plain", "").rstrip().endswith("0.0s · 0 tools")
    assert renderable.spans == []


def test_firstcoder_app_activity_metrics_are_pinned_right(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    activity.size = type("Size", (), {"width": 42})()
    app = FirstCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)

    app._start_turn_metrics()
    app._set_activity("running [=   ] · echo")
    first = activity.updates[-1]
    app._set_activity("streaming [>   ] · response")
    second = activity.updates[-1]

    assert first.endswith("0.0s · 0 tools")
    assert second.endswith("0.0s · 0 tools")
    assert first.rfind("0.0s · 0 tools") == second.rfind("0.0s · 0 tools")


def test_firstcoder_app_animates_streaming_status(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    timer = type("FakeTimer", (), {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)})()
    app = FirstCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    app._start_turn_metrics()
    app._complete_working_indicator()
    app._advance_activity_animation()

    assert activity.updates[0].startswith("streaming [>   ] · response")
    assert activity.updates[0].rstrip().endswith("0.0s · 0 tools")
    assert activity.updates[1].startswith("streaming [>>  ] · response")
    assert activity.updates[1].rstrip().endswith("0.0s · 0 tools")
    app._advance_activity_animation()
    assert activity.updates[-1].startswith("streaming [>>> ] · response")
    assert activity.updates[-1].rstrip().endswith("0.0s · 0 tools")
    assert app._activity_timer is timer


def test_firstcoder_app_does_not_restart_streaming_status_for_every_token(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    timer = type("FakeTimer", (), {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)})()
    app = FirstCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    app._start_turn_metrics()
    app._complete_working_indicator()
    after_first_token = len(activity.updates)
    app._complete_working_indicator()
    app._complete_working_indicator()

    assert after_first_token == 1
    assert len(activity.updates) == after_first_token
    assert app._activity_timer is timer


def test_firstcoder_app_hides_task_plan_panel_when_session_has_no_plan(monkeypatch) -> None:
    output = FakeOutput()
    panel = FakeTaskPlanPanel()
    view = SessionView(session_id="sess_without_plan")
    session = FakeSession()
    monkeypatch.setattr(session, "rebuild_view", lambda: view)
    app = FirstCoderApp(current_session=session)

    def query_one(selector, *args, **kwargs):
        if selector == "#task-plan-panel":
            return panel
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    app._replay_current_session()

    assert panel.updates == [""]
    assert "hidden" in panel.classes


def test_firstcoder_app_replays_linear_task_plan_from_current_session_view(monkeypatch) -> None:
    output = FakeOutput()
    panel = FakeTaskPlanPanel()
    view = SessionView(
        session_id="sess_linear_replay",
        task_plan=TaskPlan(
            mode="linear",
            revision=1,
            tasks=(
                Task(id="test", content="恢复测试", status="in_progress", order=20),
                Task(id="inspect", content="恢复代码", status="completed", order=10),
            ),
        ),
    )
    session = FakeSession()
    monkeypatch.setattr(session, "rebuild_view", lambda: view)
    app = FirstCoderApp(current_session=session)

    def query_one(selector, *args, **kwargs):
        if selector == "#task-plan-panel":
            return panel
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    app._replay_current_session()

    assert panel.updates[-1] == "Task Plan · linear\n[✓] 恢复代码\n[~] 恢复测试"


def test_firstcoder_app_replays_dag_task_plan_from_current_session_view(monkeypatch) -> None:
    output = FakeOutput()
    panel = FakeTaskPlanPanel()
    view = SessionView(
        session_id="sess_dag_replay",
        task_plan=TaskPlan(
            mode="dag",
            revision=1,
            tasks=(
                Task(id="a", content="调研 A", status="completed", order=10),
                Task(id="b", content="调研 B", status="in_progress", order=20),
                Task(id="c", content="汇总", depends_on=("a", "b"), order=30),
            ),
        ),
    )
    session = FakeSession()
    monkeypatch.setattr(session, "rebuild_view", lambda: view)
    app = FirstCoderApp(current_session=session)

    def query_one(selector, *args, **kwargs):
        if selector == "#task-plan-panel":
            return panel
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    app._replay_current_session()

    assert panel.updates[-1] == ("Task Plan · dag\n" "Level 0 · parallel\n" "  [✓] 调研 A (a)\n" "  [~] 调研 B (b)\n" "Level 1\n" "  [!] 汇总 (c) · depends on: a, b")


def test_firstcoder_app_refreshes_task_plan_from_session_instead_of_tool_result(monkeypatch) -> None:
    runner = FakeToolEventAsyncChatRunner()
    output = FakeOutput()
    activity = FakeActivity()
    panel = FakeTaskPlanPanel()
    session = FakeSession()
    monkeypatch.setattr(
        session,
        "rebuild_view",
        lambda: SessionView(
            session_id="sess_live_plan",
            task_plan=TaskPlan(
                mode="linear",
                revision=1,
                tasks=(Task(id="inspect", content="来自会话", status="in_progress"),),
            ),
        ),
    )
    app = FirstCoderApp(chat_runner=runner, current_session=session)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        if selector == "#task-plan-panel":
            return panel
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    previous_handler = app._install_tool_event_handler()
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="finished",
            tool_call=ToolCall(id="call_create", name="task_create", arguments={}),
            result=ToolResult(
                name="task_create",
                ok=True,
                content="created",
                data={"snapshot": {"tasks": [{"content": "不是展示来源"}]}},
            ),
        )
    )
    app._restore_tool_event_handler(previous_handler)

    assert panel.updates[-1] == "Task Plan · linear\n[~] 来自会话"


def test_firstcoder_app_skips_same_task_plan_revision_and_updates_existing_panel(monkeypatch) -> None:
    output = FakeOutput()
    panel = FakeTaskPlanPanel()
    app = FirstCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#task-plan-panel":
            return panel
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    initial = TaskPlan(
        mode="linear",
        revision=1,
        tasks=(Task(id="inspect", content="读代码", status="in_progress"),),
    )
    updated = TaskPlan(
        mode="linear",
        revision=2,
        tasks=(Task(id="inspect", content="读代码", status="completed"),),
    )

    app._render_task_plan_panel(initial)
    app._render_task_plan_panel(initial)
    app._render_task_plan_panel(updated)

    assert panel.updates == [
        "Task Plan · linear\n[~] 读代码",
        "Task Plan · linear\n[✓] 读代码",
    ]
    assert app.task_plan_panel_state.last_rendered_revision == 2


def test_firstcoder_app_clear_output_clears_and_hides_rendered_task_plan_panel(monkeypatch) -> None:
    output = FakeOutput()
    panel = FakeTaskPlanPanel()
    app = FirstCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#task-plan-panel":
            return panel
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "is_mounted", True)
    app._render_task_plan_panel(
        TaskPlan(
            mode="linear",
            revision=1,
            tasks=(Task(id="inspect", content="读代码", status="in_progress"),),
        )
    )

    app._clear_output()

    assert panel.updates[-1] == ""
    assert "hidden" in panel.classes
    assert app.task_plan_panel_state.last_rendered_revision is None


def test_firstcoder_app_updates_topbar_when_activity_changes(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    topbar = FakeTopbar()
    app = FirstCoderApp(current_session=FakeSession())
    monkeypatch.setattr(app, "is_mounted", True)
    monkeypatch.setattr(app, "_topbar_width", lambda: 120)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        if selector == "#topbar":
            return topbar
        return output

    monkeypatch.setattr(app, "query_one", query_one)

    app._set_activity("waiting · permission")

    assert activity.updates[0].startswith("waiting · permission")
    assert activity.updates[0].rstrip().endswith("0.0s · 0 tools")
    assert app._activity_text == "waiting · permission"
    assert "[#b28443]waiting · permission[/]" in topbar.updates[-1]


def test_firstcoder_app_live_tool_events_filter_final_tool_summary(monkeypatch) -> None:
    runner = FakeToolEventAsyncChatRunner()
    output = FakeOutput()
    app = FirstCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_handler = app._install_tool_event_handler()
    runner.tool_event_handler(ToolExecutionEvent(kind="started", tool_call=ToolCall(id="call_echo", name="echo", arguments={})))
    runner.last_display_lines = [
        'Tool call: echo {"text": "hello"}',
        "Tool result: echo success: hello",
        "done",
    ]
    app._write_chat_response(ChatResponse(provider="fake", model="fake", content="done"))
    app._restore_tool_event_handler(previous_handler)

    rendered = "\n".join(output.lines)
    assert "正在调用工具：echo" in rendered
    assert "Tool call:" not in rendered
    assert "Tool result:" not in rendered
    assert [type(widget).__name__ for widget in output.mounted] == ["Static", "FirstCoderMarkdown"]
    assert output.mounted[1].allow_select is False


def test_firstcoder_app_starts_new_stream_block_after_tool_event(monkeypatch) -> None:
    runner = FakeToolEventAsyncChatRunner()
    runner.stream_event_handler = lambda event: None
    output = FakeOutput()
    app = FirstCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_stream_handler = app._install_stream_event_handler()
    previous_tool_handler = app._install_tool_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="我先看看。"))
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="started",
            tool_call=ToolCall(id="call_echo", name="echo", arguments={}),
        )
    )
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="看完了。"))
    app._restore_tool_event_handler(previous_tool_handler)
    app._restore_stream_event_handler(previous_stream_handler)

    mounted_types = [type(widget).__name__ for widget in output.mounted]
    assert mounted_types == ["FirstCoderMarkdown", "Static", "FirstCoderMarkdown"]
    first_markdown, _, second_markdown = output.mounted
    assert first_markdown.allow_select is False
    assert second_markdown.allow_select is False
    assert first_markdown.updates[-1] == "FirstCoder:\n\n我先看看。"
    assert second_markdown.updates[-1] == "FirstCoder:\n\n看完了。"


def test_permission_requested_tool_event_uses_permission_style() -> None:
    event = ToolExecutionEvent(
        kind="permission_requested",
        tool_call=ToolCall(id="call_write", name="apply_patch", arguments={}),
    )

    assert tool_event_entry_kind(event) == TuiEntryKind.PERMISSION
    assert tool_event_status(event) == "permission_requested"
    assert tool_event_label(event) == "permission requested"
    assert (
        entry_classes(
            TuiTranscriptEntry(
                id=1,
                kind=TuiEntryKind.PERMISSION,
                body="apply_patch wants to edit firstcoder/app/tui.py",
                label="permission requested",
                status="permission_requested",
            )
        )
        == "message permission-message permission-requested"
    )


def test_firstcoder_app_renders_bypass_prewrite_review_without_permission_prompt(monkeypatch) -> None:
    runner = FakeToolEventAsyncChatRunner()
    output = FakeOutput()
    activity = FakeActivity()
    app = FirstCoderApp(chat_runner=runner)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    previous_handler = app._install_tool_event_handler()
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="prewrite_review",
            tool_call=ToolCall(id="call_write", name="write", arguments={}),
            prewrite_review={
                "tool_name": "write",
                "summary": {"created_files": 1, "modified_files": 0, "deleted_files": 0, "added_lines": 1, "removed_lines": 0},
                "files": [
                    {
                        "path": "README.md",
                        "operation": "create",
                        "diff": "--- /dev/null\n+++ b/README.md\n@@ -0,0 +1 @@\n+hello",
                        "added_lines": 1,
                        "removed_lines": 0,
                    }
                ],
            },
        )
    )
    app._restore_tool_event_handler(previous_handler)

    rendered = "\n".join(output.lines)
    assert "README.md" in rendered
    assert "+hello" in rendered
    assert "允许" not in rendered
    assert "permission" not in rendered.lower()
    assert app._activity_text != "waiting · permission"


def test_tool_skipped_has_stable_gray_tool_class() -> None:
    assert (
        entry_classes(
            TuiTranscriptEntry(
                id=1,
                kind=TuiEntryKind.TOOL,
                body="已暂停等待用户输入，跳过同批次后续工具调用。",
                label="tool shell skipped",
                status="skipped",
            )
        )
        == "message tool-message tool-skipped"
    )


def test_plain_static_renders_tool_arguments_with_markup_characters_as_text() -> None:
    content = "tool shell running\n" '  正在调用工具：shell {"cmd": "python -m pytest tests/test_app_tui.py -q", "args": ["-q"]}'
    widget = _plain_static(content, classes="message tool-message tool-running")

    rendered = widget.render()
    plain = rendered.plain if isinstance(rendered, Text) else str(rendered)

    assert plain == content


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_displays_live_tool_status_during_turn() -> None:
    runner = FakeToolEventAsyncChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()
        output = app.query_one("#output")
        text = "\n".join(str(getattr(widget, "content", getattr(widget, "renderable", ""))) for widget in output.query("Static"))

    assert "正在调用工具：echo" in text
    assert "工具完成：echo：hello" in text
    assert runner.inputs == ["hello"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_recalls_input_history_with_arrow_keys() -> None:
    runner = FakeAsyncChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"first")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press(*"second")
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press("up")
        input_widget = app.query_one("#input")
        assert input_widget.text == "second"

        await pilot.press("up")
        assert input_widget.text == "first"

        await pilot.press("down")
        assert input_widget.text == "second"

        await pilot.press("down")
        assert input_widget.text == ""

    assert runner.inputs == ["first", "second"]


def test_firstcoder_app_displays_pending_permission_prompt_immediately(monkeypatch) -> None:
    runner = FakePermissionWaitingRunner()
    output = FakeOutput()
    app = FirstCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    app._write_chat_response(ChatResponse(provider="fake", model="fake", content="等待权限确认。"))

    rendered = "\n".join([*output.lines, *(str(getattr(widget, "content", "")) for widget in output.mounted)])
    assert "permission requested  write_path README.md" in rendered
    assert "写入文件需要用户确认。" in rendered
    assert "Review before writing · 1 file · +1 -1" in rendered
    assert "-old" in rendered
    assert "+new" in rendered
    assert "[1] deny" in rendered
    assert "[2] allow once" in rendered
    assert "[3] allow always" in rendered


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_displays_chat_errors_from_worker() -> None:
    runner = FailingAsyncChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()

    assert runner.inputs == ["hello"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_rejects_chat_input_while_turn_is_running() -> None:
    runner = BlockingAsyncChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"first")
        await pilot.press("enter")
        await runner.started.wait()
        await pilot.click("#input")
        await pilot.press(*"second")
        await pilot.press("enter")
        await pilot.pause()
        runner.release.set()
        await pilot.pause()

    assert runner.inputs == ["first"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_routes_permission_answer_to_resume() -> None:
    runner = FakePermissionResumeRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"allow once")
        await pilot.press("enter")
        await pilot.pause()

    assert runner.inputs == []
    assert runner.resumes == [("perm_write", "allow_once")]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_routes_permission_rejection_feedback_to_resume() -> None:
    runner = FakePermissionResumeRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"reject: 请保留原标题")
        await pilot.press("enter")
        await pilot.pause()

    assert runner.inputs == []
    assert runner.resumes == [("perm_write", "reject_with_feedback: 请保留原标题")]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_permission_resume_keeps_same_active_turn_metrics(monkeypatch) -> None:
    runner = FakePermissionMidTurnRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"write readme")
        await pilot.press("enter")
        await pilot.pause()

        active_turn = app._active_chat_turn
        assert active_turn is not None
        assert app._chat_busy is False
        started_at = app._turn_started_at
        assert started_at > 0

        app._turn_tool_count = 2
        await pilot.press(*"allow once")
        await pilot.press("enter")
        await pilot.pause()

    assert runner.inputs == ["write readme"]
    assert runner.resumes == [("perm_write", "allow_once")]
    assert app._active_chat_turn is None
    assert app._turn_started_at == started_at
    assert app._turn_tool_count == 2
