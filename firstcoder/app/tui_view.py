"""Rendering, activity, and streaming helpers for the Textual TUI."""

from __future__ import annotations

import threading

from rich.text import Text
from textual.css.query import NoMatches

from firstcoder.app import yuren_topbar_themes
from firstcoder.app.activity_view import (
    activity_markup,
    post_tool_reasoning_text,
    task_plan_panel_text,
    tool_activity_line_text,
    tool_activity_summary,
    tool_event_label,
    tool_event_status,
    tool_status_text,
    truncate_activity_text,
    turn_metrics_text,
)
from firstcoder.app.permission_view import permission_prompt_text
from firstcoder.app.review_view import render_prewrite_review
from firstcoder.app.topbar_view import (
    PERMISSION_MODE_COLORS,
    _metadata_markup,
    _markup_width,
    _provider_model_markup,
    _truncate_markup,
)
from firstcoder.app.transcript_view import (
    entry_classes,
    entry_markdown_text,
    entry_plain_text,
    tool_event_entry_kind,
)
from firstcoder.app.tui_state import TuiEntryKind, TuiTranscriptEntry
from firstcoder.app.tui_widgets import FirstCoderMarkdown, _observe_markdown_update, _plain_static
from firstcoder.app.welcome import welcome_renderable
from firstcoder.planning.models import TaskPlan
from firstcoder.planning.projection import project_plan
from firstcoder.tools.hidden import HIDDEN_TOOL_STATUS_NAMES


def _entry_renderable(entry: TuiTranscriptEntry, rendered: str) -> object:
    if entry.kind != TuiEntryKind.COMMAND:
        return rendered
    if not any(line.startswith("> ") for line in rendered.splitlines()):
        return rendered
    text = Text()
    for line_index, line in enumerate(rendered.splitlines()):
        if line_index:
            text.append("\n")
        if line.startswith("> "):
            text.append(">", style="#7bba55 bold")
            text.append(line[1:])
        else:
            text.append(line)
    return text


class FirstCoderViewMixin:

    def _refresh_session_subtitle(self) -> None:
        session_id = None
        if self.current_session is None:
            self.sub_title = ""
        else:
            session_id = self.current_session.session_id
            self.sub_title = f"Session: {session_id}"
        topbar = self._query_mounted("#topbar")
        if topbar is not None and hasattr(topbar, "update"):
            topbar.update(self._topbar_text(session_id=session_id, width=self._topbar_width()))

    def _topbar_width(self) -> int | None:
        size = getattr(self, "size", None)
        width = getattr(size, "width", None)
        if isinstance(width, int) and width > 0:
            return max(1, width - 4)
        return None

    def _topbar_text(self, *, session_id: str | None = None, width: int | None = None) -> str:
        if session_id is None and self.current_session is not None:
            session_id = self.current_session.session_id
        brand = "[#7bba55]FirstCoder[/]"
        status = activity_markup(self._activity_text)
        metadata_values: list[tuple[str | None, str, int | None]] = []
        if self.config.provider_name or self.config.provider_model:
            provider = self.config.provider_name or "provider"
            model = self.config.provider_model or "model"
            metadata_values.append(
                (
                    None,
                    _provider_model_markup(provider, model, glow_frame=self._provider_glow_frame),
                    18,
                )
            )
        mode = getattr(self.current_session, "mode", None) if self.current_session is not None else None
        if mode:
            mode_text = str(mode)
            mode_color = PERMISSION_MODE_COLORS.get(mode_text, "#6e6d72")
            metadata_values.append((mode_color, mode_text, None))
        if self.config.project_name:
            metadata_values.append(("#6e6d72", f"cwd {self.config.project_name}", 22))
        top_separator = "   [#303238]·[/]   "
        metadata = _metadata_markup(metadata_values, separator=top_separator)
        compact = f"{brand}{top_separator}{status}{top_separator}{metadata}"
        if width is None:
            return compact
        brand_width = _markup_width(brand)
        status_width = _markup_width(status)
        metadata_width = _markup_width(metadata)
        top_separator_width = _markup_width(top_separator) * 2
        content_width = brand_width + status_width + metadata_width + top_separator_width
        if content_width > width or status_width > max(1, width - brand_width - metadata_width - top_separator_width):
            fixed_width = brand_width + metadata_width + top_separator_width
            if fixed_width >= width:
                metadata_width = max(0, width - brand_width - top_separator_width - 8)
                metadata = _truncate_markup(metadata, metadata_width)
                fixed_width = brand_width + _markup_width(metadata) + top_separator_width
            available_status_width = max(0, width - fixed_width)
            status = activity_markup(truncate_activity_text(self._activity_text, available_status_width))
            compact = f"{brand}{top_separator}{status}{top_separator}{metadata}"
            return _truncate_markup(compact, width) if _markup_width(compact) > width else compact
        if width - content_width < 8:
            available_status_width = width - brand_width - metadata_width - top_separator_width
            if available_status_width < status_width:
                status = activity_markup(truncate_activity_text(self._activity_text, max(1, available_status_width)))
                compact = f"{brand}{top_separator}{status}{top_separator}{metadata}"
            return compact
        left_gap = max(3, (width // 2) - _markup_width(brand) - (_markup_width(status) // 2))
        right_gap = width - brand_width - left_gap - _markup_width(status) - metadata_width
        if right_gap < 3:
            right_gap = 3
            left_gap = width - brand_width - _markup_width(status) - metadata_width - right_gap
        return f"{brand}{' ' * left_gap}{status}{' ' * right_gap}{metadata}"

    def _install_stream_event_handler(self, token: int | None = None):
        if self.chat_runner is None or not hasattr(self.chat_runner, "stream_event_handler"):
            return None
        previous_handler = getattr(self.chat_runner, "stream_event_handler", None)
        self._stream_reasoning_started = False
        self._stream_text_started = False
        self._stream_text_needs_newline = False
        self._stream_text_buffer = ""
        self._stream_text_widget = None
        self._stream_markdown_finalized = False
        self._stream_text_entry = None
        self._stream_rendered_text = ""
        self._stream_flush_timer = None
        self._reasoning_buffer = ""
        self._reasoning_is_fallback = False
        self._working_text = ""
        self._working_frame_index = 0
        self._stop_working_animation()
        self._stream_segment_closed_for_tool = False

        def handle_event(event) -> None:
            if previous_handler is not None:
                previous_handler(event)
            if token is not None and not self._is_current_chat_turn(token):
                return
            kind = getattr(event, "kind", None)
            text = getattr(event, "text", "") or ""
            if not text:
                return
            if kind == "reasoning_delta":
                self._stream_reasoning_started = True
                self._call_ui_thread(self._append_reasoning_text, text)
            elif kind == "text_delta":
                self._stream_text_started = True
                self._stream_text_needs_newline = True
                self._call_ui_thread(self._complete_working_indicator)
                self._call_ui_thread(self._append_stream_text, text)

        setattr(self.chat_runner, "stream_event_handler", handle_event)
        return previous_handler

    def _restore_stream_event_handler(self, previous_handler) -> None:
        self._restore_runner_handler("stream_event_handler", previous_handler)

    def _install_tool_event_handler(self, token: int | None = None):
        if self.chat_runner is None or not hasattr(self.chat_runner, "tool_event_handler"):
            return None
        previous_handler = getattr(self.chat_runner, "tool_event_handler", None)
        self._live_tool_events_seen = False

        def handle_event(event) -> None:
            if previous_handler is not None:
                previous_handler(event)
            if token is not None and not self._is_current_chat_turn(token):
                return
            tool_call = getattr(event, "tool_call", None)
            tool_name = str(getattr(tool_call, "name", "") or "tool")
            if tool_name in HIDDEN_TOOL_STATUS_NAMES:
                return
            if str(getattr(event, "kind", "") or "") == "prewrite_review":
                review = getattr(event, "prewrite_review", None)
                if isinstance(review, dict):
                    self._call_ui_thread(self._write_review_payload, review)
                return
            line = tool_status_text(event)
            if not line:
                return
            self._live_tool_events_seen = True
            self._call_ui_thread(self._close_stream_segment_for_tool)
            self._call_ui_thread(self._record_tool_activity, event)
            if tool_name in {"task_create", "task_update", "task_revise"} and str(getattr(event, "kind", "") or "") == "finished":
                self._call_ui_thread(self._refresh_task_plan_panel_from_current_session)
            self._call_ui_thread(
                self._write_line,
                line,
                kind=tool_event_entry_kind(event),
                label=tool_event_label(event),
                status=tool_event_status(event),
            )

        setattr(self.chat_runner, "tool_event_handler", handle_event)
        return previous_handler

    def _restore_tool_event_handler(self, previous_handler) -> None:
        self._restore_runner_handler("tool_event_handler", previous_handler)

    def _restore_runner_handler(self, attr: str, previous_handler) -> None:
        if self.chat_runner is not None and hasattr(self.chat_runner, attr):
            setattr(self.chat_runner, attr, previous_handler)

    def _call_ui_thread(self, callback, *args, **kwargs):
        if not getattr(self, "is_running", False):
            return callback(*args, **kwargs)
        if getattr(self, "_thread_id", None) == threading.get_ident():
            return callback(*args, **kwargs)
        return self.call_from_thread(callback, *args, **kwargs)

    def _scroll_output_end_if_pinned(self, output) -> None:
        if not hasattr(output, "scroll_end"):
            return
        scroll_y = float(getattr(output, "scroll_y", 0) or 0)
        max_scroll_y = float(getattr(output, "max_scroll_y", 0) or 0)
        if max_scroll_y and scroll_y < max_scroll_y - 1:
            return
        output.scroll_end(animate=False)

    def _write_line(
        self,
        text: str,
        *,
        classes: str | None = None,
        kind: TuiEntryKind = TuiEntryKind.SYSTEM,
        label: str | None = None,
        status: str | None = None,
    ) -> TuiTranscriptEntry:
        entry = self.transcript.add(kind, text, label=label, status=status)
        classes = classes or entry_classes(entry)
        rendered = entry_plain_text(entry)
        output = self.query_one("#output")
        if hasattr(output, "mount"):
            widget = _plain_static(_entry_renderable(entry, rendered), classes=classes)
            entry.widget = widget
            output.mount(widget)
            self._scroll_output_end_if_pinned(output)
            return entry
        if hasattr(output, "write_line"):
            output.write_line(rendered)
        return entry

    def _show_welcome(self) -> None:
        output = self.query_one("#output")
        if not hasattr(output, "mount"):
            return
        if hasattr(output, "add_class"):
            output.add_class("welcome-active")
        self._welcome_widget = _plain_static(
            welcome_renderable(compact=self._uses_compact_welcome()),
            id="welcome",
            classes="welcome",
        )
        output.mount(self._welcome_widget)
        if not self._uses_compact_welcome():
            self._start_welcome_particles()

    def _dismiss_welcome(self) -> None:
        self._stop_welcome_particles()
        output = self._query_mounted("#output")
        if output is not None and hasattr(output, "remove_class"):
            output.remove_class("welcome-active")
        widget = self._welcome_widget
        self._welcome_widget = None
        if widget is None:
            return
        remove = getattr(widget, "remove", None)
        if remove is not None:
            remove()

    def _start_interval_timer(self, attr: str, interval: float, callback, *, name: str) -> None:
        if getattr(self, attr) is not None or getattr(self, "_loop", None) is None:
            return
        setattr(self, attr, self.set_interval(interval, callback, name=name))

    def _stop_interval_timer(self, attr: str) -> None:
        timer = getattr(self, attr, None)
        if timer is None:
            return
        timer.stop()
        setattr(self, attr, None)

    def _start_welcome_particles(self) -> None:
        self._start_interval_timer(
            "_welcome_particle_timer",
            self.WELCOME_PARTICLE_INTERVAL_SECONDS,
            self._advance_welcome_particles,
            name="welcome-particles",
        )

    def _stop_welcome_particles(self) -> None:
        self._stop_interval_timer("_welcome_particle_timer")

    def _advance_welcome_particles(self) -> None:
        if self._welcome_widget is None:
            self._stop_welcome_particles()
            return
        if self._uses_compact_welcome():
            self._stop_welcome_particles()
            return
        self._welcome_particle_frame += 1
        self._welcome_widget.update(welcome_renderable(particle_frame=self._welcome_particle_frame))

    def _uses_compact_welcome(self) -> bool:
        size = getattr(self, "size", None)
        width = getattr(size, "width", None)
        height = getattr(size, "height", None)
        return bool(isinstance(width, int) and isinstance(height, int) and (width <= self.COMPACT_WELCOME_MAX_WIDTH or height <= self.COMPACT_WELCOME_MAX_HEIGHT))

    def _refresh_welcome_layout(self) -> None:
        widget = self._welcome_widget
        if widget is None:
            return
        compact = self._uses_compact_welcome()
        widget.update(welcome_renderable(compact=compact, particle_frame=self._welcome_particle_frame))
        if compact:
            self._stop_welcome_particles()
        else:
            self._start_welcome_particles()

    def _sync_provider_glow(self) -> None:
        if yuren_topbar_themes.should_animate(self.config.provider_name, self.config.provider_model):
            self._start_provider_glow()
        else:
            self._stop_provider_glow()

    def _start_provider_glow(self) -> None:
        self._start_interval_timer(
            "_provider_glow_timer",
            self.PROVIDER_GLOW_INTERVAL_SECONDS,
            self._advance_provider_glow,
            name="yuren-provider-glow",
        )

    def _stop_provider_glow(self) -> None:
        self._stop_interval_timer("_provider_glow_timer")

    def _advance_provider_glow(self) -> None:
        palette = yuren_topbar_themes.model_glow_palette(
            self.config.provider_name,
            self.config.provider_model,
        )
        if palette is None:
            self._stop_provider_glow()
            return
        self._provider_glow_frame = (self._provider_glow_frame + 1) % len(palette)
        self._refresh_topbar()

    def _record_tool_activity(self, event) -> None:
        tool_call = getattr(event, "tool_call", None)
        name = str(getattr(tool_call, "name", "") or "tool")
        status = tool_event_status(event) or "unknown"
        tool_call_id = str(getattr(tool_call, "id", "") or "")
        if status == "running":
            self._turn_tool_count += 1
            if tool_call_id:
                self._running_tool_call_ids.add(tool_call_id)
        elif tool_call_id:
            self._running_tool_call_ids.discard(tool_call_id)
        summary = tool_activity_summary(event)
        self.transcript.record_tool_activity(name, status, summary)
        if status == "success":
            self._show_working_indicator(post_tool_reasoning_text(name))
            return
        self._stop_working_animation()
        if status == "running":
            self._show_activity_animation("running", self._running_tools_activity_detail(name))
            return
        self._show_static_activity(tool_activity_line_text(name, status))

    def _refresh_task_plan_panel_from_current_session(self) -> None:
        current_session = self.current_session
        if current_session is None:
            return
        rebuild_view = getattr(current_session, "rebuild_view", None)
        if rebuild_view is None:
            return
        view = rebuild_view()
        self._render_task_plan_panel(view.task_plan)

    def _clear_task_plan_panel_if_mounted(self) -> None:
        panel = self._query_mounted("#task-plan-panel")
        if panel is None:
            return
        panel.update("")
        if hasattr(panel, "add_class"):
            panel.add_class("hidden")

    def _render_task_plan_panel(self, task_plan: TaskPlan | None) -> None:
        panel = self.query_one("#task-plan-panel")
        if task_plan is None:
            panel.update("")
            if hasattr(panel, "add_class"):
                panel.add_class("hidden")
            self.task_plan_panel_state.last_rendered_revision = None
            return
        if self.task_plan_panel_state.last_rendered_revision == task_plan.revision:
            return
        if hasattr(panel, "remove_class"):
            panel.remove_class("hidden")
        panel.update(task_plan_panel_text(project_plan(task_plan)))
        self.task_plan_panel_state.last_rendered_revision = task_plan.revision

    def _write_markdown_message(self, content: str, *, classes: str = "message assistant-message") -> None:
        entry = self.transcript.add(TuiEntryKind.ASSISTANT, content)
        text = entry_markdown_text(entry)
        output = self.query_one("#output")
        if hasattr(output, "mount"):
            markdown = FirstCoderMarkdown(classes=classes)
            output.mount(markdown)
            _observe_markdown_update(markdown.update(text))
            self._scroll_output_end_if_pinned(output)
            return
        if hasattr(output, "write_line"):
            output.write_line(text)

    def _write_pending_input(self) -> None:
        pending = getattr(self.chat_runner, "last_pending_input", None)
        if pending is None:
            return
        if getattr(pending, "kind", None) == "permission_confirmation":
            payload = getattr(pending, "payload", {}) or {}
            review_payload = payload.get("prewrite_review")
            if isinstance(review_payload, dict):
                self._review_expanded_paths.clear()
                self._write_review_payload(review_payload)
            self._write_line(permission_prompt_text(pending), kind=TuiEntryKind.PERMISSION)
            self._set_activity("waiting · permission")
            return
        question = str(getattr(pending, "question", "") or "需要用户输入。")
        self._write_line(f"需要用户输入：\n{question}", kind=TuiEntryKind.PERMISSION)
        self._set_activity("waiting · input")

    def _write_review_payload(self, payload: dict[str, object]) -> None:
        rendered = render_prewrite_review(payload, expanded_paths=self._review_expanded_paths)
        output = self.query_one("#output")
        if hasattr(output, "mount"):
            output.mount(_plain_static(rendered, classes="message permission-message review-message"))
            self._scroll_output_end_if_pinned(output)
            return
        if hasattr(output, "write_line"):
            output.write_line(rendered.plain)

    def _show_working_indicator(self, text: str) -> None:
        self._stop_activity_animation()
        self._reasoning_buffer = text
        self._reasoning_is_fallback = True
        self._working_text = text
        self._working_frame_index = 0
        self._set_activity(self._working_indicator_body())
        self._start_working_animation()

    def _complete_working_indicator(self) -> None:
        if self._activity_animation_kind == "streaming" and self._activity_animation_detail == "response":
            return
        self._stop_working_animation()
        self._show_activity_animation("streaming", "response")

    def _append_reasoning_text(self, text: str) -> None:
        if self._reasoning_is_fallback:
            self._reasoning_buffer = ""
            self._reasoning_is_fallback = False
            self._working_text = ""
        self._reasoning_buffer += text
        self._set_activity(self._working_indicator_body(self._reasoning_buffer))
        self._start_working_animation()

    def _working_indicator_body(self, text: str | None = None) -> str:
        frame = self.WORKING_FRAMES[self._working_frame_index % len(self.WORKING_FRAMES)]
        return f"thinking {frame} {text if text is not None else self._working_text}"

    def _start_working_animation(self) -> None:
        self._start_interval_timer(
            "_working_timer",
            self.WORKING_ANIMATION_INTERVAL_SECONDS,
            self._advance_working_animation,
            name="working-indicator",
        )

    def _stop_working_animation(self) -> None:
        self._stop_interval_timer("_working_timer")

    def _advance_working_animation(self) -> None:
        self._working_frame_index += 1
        text = self._working_text or self._reasoning_buffer
        self._set_activity(self._working_indicator_body(text))

    def _show_static_activity(self, text: str) -> None:
        self._show_activity_animation("static", text)

    def _activity_animation_body(self) -> str:
        if self._activity_animation_kind == "static":
            return self._activity_animation_detail
        frames = self.ACTIVITY_FRAMES.get(self._activity_animation_kind) or ("[....]",)
        frame = frames[self._activity_frame_index % len(frames)]
        return f"{self._activity_animation_kind} {frame} · {self._activity_animation_detail}"

    def _preserve_turn_metrics(self) -> None:
        if not self._turn_started_at:
            self._start_turn_metrics()

    def _running_tools_activity_detail(self, fallback_name: str) -> str:
        running_count = len(self._running_tool_call_ids)
        if running_count > 1:
            return f"{running_count} tools running"
        return fallback_name

    def _start_activity_animation(self) -> None:
        self._start_interval_timer(
            "_activity_timer",
            self.ACTIVITY_ANIMATION_INTERVAL_SECONDS,
            self._advance_activity_animation,
            name="activity-indicator",
        )

    def _stop_activity_animation(self) -> None:
        self._stop_interval_timer("_activity_timer")
        self._activity_animation_kind = ""
        self._activity_animation_detail = ""

    def _advance_activity_animation(self) -> None:
        if not self._activity_animation_kind:
            return
        self._activity_frame_index += 1
        self._set_activity(self._activity_animation_body())

    def _query_mounted(self, selector: str):
        if not getattr(self, "is_mounted", False):
            return None
        try:
            return self.query_one(selector)
        except NoMatches:
            return None

    def _set_activity(self, text: str) -> None:
        self._activity_text = text
        activity = self._query_mounted("#activity")
        if activity is None:
            return
        rendered = self.tool_activity_line_text(text, activity)
        if hasattr(activity, "update"):
            activity.update(self._activity_renderable(rendered))
        self._refresh_topbar()

    def _refresh_topbar(self) -> None:
        topbar = self._query_mounted("#topbar")
        if topbar is not None and hasattr(topbar, "update"):
            topbar.update(self._topbar_text(width=self._topbar_width()))

    def _activity_renderable(self, text: str) -> Text:
        return Text(text, style="#527c3b")

    def tool_activity_line_text(self, text: str, activity) -> str:
        metrics = turn_metrics_text(self._turn_elapsed_seconds(), self._turn_tool_count)
        width = getattr(getattr(activity, "size", None), "width", None)
        if not isinstance(width, int) or width <= 0:
            return f"{text} · {metrics}" if text != "idle · ready" else text
        if text == "idle · ready":
            return text
        if len(text) + len(metrics) + 1 > width:
            available = max(1, width - len(metrics) - 1)
            text = truncate_activity_text(text, available)
        return f"{text}{' ' * (width - len(text) - len(metrics))}{metrics}"

    def _append_stream_text(self, text: str) -> None:
        if self._stream_segment_closed_for_tool:
            self._start_new_stream_segment()
        self._stream_text_buffer += text
        if self._stream_text_entry is None:
            self._stream_text_entry = self.transcript.add(TuiEntryKind.ASSISTANT, self._stream_text_buffer)
        else:
            self._stream_text_entry.body = self._stream_text_buffer
        output = self.query_one("#output")
        if hasattr(output, "mount"):
            if self._stream_text_widget is None:
                self._stream_text_widget = FirstCoderMarkdown(classes="message assistant-message streaming")
                output.mount(self._stream_text_widget)
            if not self._stream_rendered_text:
                self._flush_stream_text()
            else:
                self._schedule_stream_flush()
            return
        if hasattr(output, "write"):
            prefix = "FirstCoder:\n" if self._stream_text_buffer == text else ""
            output.write(f"{prefix}{text}")

    def _close_stream_segment_for_tool(self) -> None:
        if self._stream_text_widget is None and not self._stream_text_buffer:
            return
        self._flush_stream_text()
        self._stream_segment_closed_for_tool = True

    def _start_new_stream_segment(self) -> None:
        self._stream_text_buffer = ""
        self._stream_text_widget = None
        self._stream_text_entry = None
        self._stream_rendered_text = ""
        self._stream_flush_timer = None
        self._stream_segment_closed_for_tool = False

    def _schedule_stream_flush(self) -> None:
        if self._stream_flush_timer is not None:
            return
        if getattr(self, "_loop", None) is None:
            return
        self._stream_flush_timer = self.set_timer(
            self.STREAM_RENDER_INTERVAL_SECONDS,
            self._flush_stream_text,
            name="stream-markdown-flush",
        )

    def _flush_stream_text(self) -> bool:
        self._stream_flush_timer = None
        if self._stream_text_widget is None:
            return False
        if self._stream_rendered_text == self._stream_text_buffer:
            return False
        self._stream_rendered_text = self._stream_text_buffer
        _observe_markdown_update(self._stream_text_widget.update(f"FirstCoder:\n\n{self._stream_rendered_text}"))
        output = self.query_one("#output")
        self._scroll_output_end_if_pinned(output)
        return True
