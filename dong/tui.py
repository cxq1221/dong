"""Fullscreen prompt_toolkit TUI for dong interactive mode."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from io import StringIO
from queue import Empty, Queue

from prompt_toolkit.application import Application
from prompt_toolkit.completion import Completer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from dong.tool import ToolResult
from dong.ui import (
    DONG_LIGHT_THEME,
    MARKDOWN_CODE_THEME,
    _SlashAwareCompleter,
    _assistant_display_text,
    _normalize_model_text,
)


@dataclass
class TranscriptItem:
    """A stable transcript entry rendered inside the fullscreen TUI."""

    kind: str
    title: str
    raw: str
    ansi: str
    streaming: bool = False
    created_at: float = field(default_factory=time.monotonic)


@dataclass
class StatusState:
    """Current TUI status bar state."""

    label: str = "idle"
    queued: int = 0
    new_output: bool = False
    cancellation_requested: bool = False


@dataclass
class ConfirmationRequest:
    """A synchronous confirmation owned by the active agent/tool turn."""

    command: str
    default: str
    done: threading.Event = field(default_factory=threading.Event)
    result: bool | None = None


class _TranscriptControl(FormattedTextControl):
    """Transcript 专用 control：把鼠标滚轮映射到 dong 自己的历史视图。"""

    def __init__(self, app: TuiApp) -> None:
        self.app = app
        super().__init__(
            app._formatted_transcript,
            get_cursor_position=app._transcript_cursor_position,
        )

    def invalidate_content(self) -> None:
        """清理 prompt_toolkit 的片段缓存，确保滚动切片立即重算。"""
        self.reset()
        self._fragment_cache.clear()
        self._content_cache.clear()

    def mouse_handler(self, mouse_event: MouseEvent):  # type: ignore[no-untyped-def]
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self.app.scroll_transcript(3)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self.app.scroll_transcript(-3)
            return None
        return super().mouse_handler(mouse_event)


class _DynamicSlashCompleter(Completer):
    """Prompt-toolkit completer that refreshes candidates for every completion request."""

    def __init__(self, completion_provider: Callable[[], Iterable[str]]) -> None:
        self.completion_provider = completion_provider

    def get_completions(self, document, complete_event):  # type: ignore[no-untyped-def]
        completer = _SlashAwareCompleter(sorted({item for item in self.completion_provider() if item}))
        yield from completer.get_completions(document, complete_event)


class _TuiConsoleProxy:
    """Small Console-like proxy for legacy code paths that still call ui.err_console.print."""

    def __init__(self, ui: TuiUI) -> None:
        self.ui = ui

    def print(self, *objects: object, **_: object) -> None:
        text = " ".join(str(obj) for obj in objects)
        self.ui.show_system_message(text)


class TuiApp:
    """Owns the fullscreen prompt_toolkit application and worker queue."""

    def __init__(
        self,
        *,
        process_input: Callable[[str, "TuiUI"], bool],
        completion_provider: Callable[[], Iterable[str]],
        title: str = "dong",
    ) -> None:
        self.process_input = process_input
        self.completion_provider = completion_provider
        self.title = title
        self.ui = TuiUI(self)

        self._lock = threading.RLock()
        self._input_queue: Queue[str | None] = Queue()
        self._worker_errors: list[BaseException] = []
        self._worker_thread = threading.Thread(
            target=self._worker,
            name="dong-tui-worker",
            daemon=False,
        )
        self._running = False
        self._busy = False
        self._follow_bottom = True
        self._scroll_offset = 0
        self._transcript_total_line_count = 1
        self._transcript_cursor_line = 0
        self._last_ctrl_c = 0.0
        self._render_width_override: int | None = None
        self._confirmation: ConfirmationRequest | None = None
        self._transcript: list[TranscriptItem] = []
        self._status = StatusState()

        self.composer = TextArea(
            multiline=True,
            completer=_DynamicSlashCompleter(self.completion_provider),
            complete_while_typing=True,
            history=InMemoryHistory(),
            prompt="dong > ",
            height=3,
            wrap_lines=True,
            read_only=Condition(lambda: self._confirmation is not None),
            style="class:composer",
        )
        self.key_bindings = self._key_bindings()
        self.composer.control.key_bindings = self.key_bindings
        self.transcript_control = _TranscriptControl(self)
        self.status_control = FormattedTextControl(self._formatted_status)
        self.application = Application(
            layout=Layout(
                HSplit([
                    Window(
                        content=self.transcript_control,
                        wrap_lines=True,
                        always_hide_cursor=True,
                    ),
                    Window(
                        content=self.status_control,
                        height=1,
                        always_hide_cursor=True,
                        style="reverse",
                    ),
                    self.composer,
                ]),
                focused_element=self.composer,
            ),
            key_bindings=self.key_bindings,
            full_screen=True,
            mouse_support=True,
        )

    @property
    def transcript_text(self) -> str:
        """Return raw transcript text for tests and diagnostics."""
        with self._lock:
            return "\n".join(item.raw for item in self._transcript)

    @property
    def status(self) -> StatusState:
        """Return a copy-like view of current status state."""
        with self._lock:
            return StatusState(
                label=self._status.label,
                queued=self._status.queued,
                new_output=self._status.new_output,
                cancellation_requested=self._status.cancellation_requested,
            )

    @property
    def render_width(self) -> int:
        """Current transcript render width, bounded for narrow terminals."""
        if self._render_width_override is not None:
            return max(20, self._render_width_override)
        try:
            columns = self.application.output.get_size().columns
        except Exception:
            columns = 80
        return max(20, columns - 2)

    def run(self) -> None:
        """Run the fullscreen TUI until the user exits."""
        self._running = True
        self._worker_thread.start()
        try:
            self.application.run()
        finally:
            self._running = False
            self._discard_pending_inputs()
            self._input_queue.put(None)
            self._worker_thread.join(timeout=2.0)
            if self._worker_errors:
                raise self._worker_errors[0]

    def submit_text(self, text: str) -> None:
        """Submit composer text to the worker queue."""
        text = text.strip()
        if not text:
            return
        with self._lock:
            self._input_queue.put(text)
            self._status.queued = self._input_queue.qsize()
            self._status.new_output = False
            self._follow_bottom = True
        self.invalidate()

    def append_item(self, kind: str, title: str, ansi: str, raw: str | None = None) -> TranscriptItem:
        """Append a rendered item to the transcript."""
        item = TranscriptItem(kind=kind, title=title, raw=raw if raw is not None else ansi, ansi=ansi)
        with self._lock:
            self._transcript.append(item)
            if not self._follow_bottom:
                self._status.new_output = True
            self.transcript_control.invalidate_content()
        self.invalidate()
        return item

    def update_item(self, item: TranscriptItem, *, ansi: str, raw: str | None = None) -> None:
        """Update an existing transcript item in place."""
        with self._lock:
            item.ansi = ansi
            if raw is not None:
                item.raw = raw
            if not self._follow_bottom:
                self._status.new_output = True
            self.transcript_control.invalidate_content()
        self.invalidate()

    def update_status(self, label: str) -> None:
        """Update the status bar text."""
        with self._lock:
            self._status.label = label
        self.invalidate()

    def clear_status(self) -> None:
        """Return status bar to idle/queued state."""
        with self._lock:
            self._status.label = "idle"
            self._status.cancellation_requested = False
        self.invalidate()

    def request_confirmation(self, command: str, default: str) -> bool:
        """Show a synchronous dangerous-command confirmation prompt."""
        request = ConfirmationRequest(command=command, default=default.lower())
        with self._lock:
            self._confirmation = request
            self._status.label = f"Dangerous command: {command}  Run command? [y/N]"
        self.invalidate()
        request.done.wait()
        with self._lock:
            self._confirmation = None
            self._status.label = "idle" if not self._busy else "working"
        self.invalidate()
        return bool(request.result)

    def answer_confirmation(self, value: bool) -> None:
        """Answer the active confirmation prompt."""
        with self._lock:
            request = self._confirmation
        if request is None:
            return
        request.result = value
        request.done.set()

    def request_cancel(self) -> None:
        """Request cooperative cancellation of the active worker operation."""
        with self._lock:
            self._status.cancellation_requested = True
            self._status.label = "cancellation requested"
        self.invalidate()

    def invalidate(self) -> None:
        """Thread-safe redraw request."""
        if self._running:
            self.application.invalidate()

    def get_last_assistant_raw(self) -> str | None:
        """Return raw text of the last assistant transcript item."""
        with self._lock:
            for item in reversed(self._transcript):
                if item.kind == "assistant" and item.raw.strip():
                    return item.raw
        return None

    def scroll_transcript(self, lines: int) -> None:
        """Scroll transcript history; positive lines move to older output."""
        if lines == 0:
            return
        with self._lock:
            max_offset = max(0, self._transcript_total_line_count - 1)
            if lines > 0:
                self._follow_bottom = False
                self._scroll_offset = min(max_offset, self._scroll_offset + lines)
            else:
                self._scroll_offset = max(0, self._scroll_offset + lines)
                if self._scroll_offset == 0:
                    self._follow_bottom = True
                    self._status.new_output = False
            self.transcript_control.invalidate_content()
        self.invalidate()

    def _worker(self) -> None:
        while True:
            item = self._input_queue.get()
            try:
                if item is None:
                    return
                with self._lock:
                    self._busy = True
                    self._status.queued = self._input_queue.qsize()
                    self._status.label = "working"
                self.invalidate()
                exit_requested = self.process_input(item, self.ui)
                if exit_requested:
                    self.application.exit()
                    return
            except BaseException as exc:  # pragma: no cover - surfaced by run()
                self._worker_errors.append(exc)
                try:
                    self.application.exit()
                except Exception:
                    pass
                return
            finally:
                with self._lock:
                    self._busy = False
                    self._status.queued = self._input_queue.qsize()
                    if self._status.label == "working":
                        self._status.label = "idle"
                self._input_queue.task_done()
                self.invalidate()

    def _discard_pending_inputs(self) -> None:
        while True:
            try:
                self._input_queue.get_nowait()
            except Empty:
                return
            self._input_queue.task_done()

    def _formatted_transcript(self) -> ANSI:
        with self._lock:
            lines = self._render_transcript_lines()
            if lines:
                text = "\n".join(lines)
                self._transcript_cursor_line = max(0, len(lines) - 1)
            else:
                text = f"\x1b[36m{self.title}\x1b[0m"
                self._transcript_total_line_count = 1
                self._transcript_cursor_line = 0
        return ANSI(text)

    def _transcript_cursor_position(self) -> Point:
        """Keep prompt_toolkit's window anchored to the bottom of the current transcript slice."""
        with self._lock:
            return Point(x=0, y=max(0, self._transcript_cursor_line))

    def _formatted_status(self) -> ANSI:
        with self._lock:
            parts = [self._status.label]
            if self._busy:
                parts.append("working")
            if self._status.queued:
                parts.append(f"queued {self._status.queued}")
            if self._status.new_output:
                parts.append("new output")
            if self._status.cancellation_requested:
                parts.append("cancel requested")
            if self._confirmation is not None:
                parts.append("confirm y/N")
            completion_hint = self._completion_hint()
            if completion_hint:
                parts.append(completion_hint)
        return ANSI("  " + _fit_to_width(" · ".join(parts), self.render_width))

    def _completion_hint(self) -> str:
        text = self.composer.text
        if not text.startswith("/"):
            return ""
        completer = _SlashAwareCompleter(sorted({item for item in self.completion_provider() if item}))
        candidates = [
            completion.text
            for completion in completer.get_completions(Document(text), None)
        ][:6]
        return " ".join(candidates)

    def _render_transcript_lines(self) -> list[str]:
        text = "\n\n".join(item.ansi.rstrip() for item in self._transcript if item.ansi.strip())
        lines = text.splitlines()
        self._transcript_total_line_count = max(1, len(lines))
        if self._follow_bottom:
            return lines[-2000:]
        if self._scroll_offset <= 0:
            return lines[-2000:]
        end = max(0, len(lines) - self._scroll_offset)
        return lines[max(0, end - 2000):end]

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("enter")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            if self._confirmation is not None:
                self.answer_confirmation(False)
                return
            text = self.composer.text
            self.composer.buffer.reset()
            self.submit_text(text)

        @bindings.add("c-j")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            event.app.current_buffer.insert_text("\n")

        @bindings.add("/")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            if self._confirmation is not None:
                return
            event.app.current_buffer.insert_text("/")
            if event.app.current_buffer.document.text_before_cursor == "/":
                event.app.current_buffer.start_completion(select_first=False)

        @bindings.add("c-c", eager=True)
        def _(event) -> None:  # type: ignore[no-untyped-def]
            if self._confirmation is not None:
                self.answer_confirmation(False)
                return
            if self.composer.text:
                self.composer.buffer.reset()
                return
            with self._lock:
                busy = self._busy
            if busy:
                self.request_cancel()
                return
            now = time.monotonic()
            if now - self._last_ctrl_c < 2.0:
                event.app.exit()
                return
            self._last_ctrl_c = now
            self.ui.show_warning("再按一次 Ctrl-C 退出，或 Ctrl-D 直接退出。")

        @bindings.add("c-d", eager=True)
        def _(event) -> None:  # type: ignore[no-untyped-def]
            event.app.exit()

        @bindings.add("c-y")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            if self._confirmation is not None:
                return
            raw = self.get_last_assistant_raw()
            if raw is not None:
                event.app.current_buffer.insert_text(raw)

        @bindings.add("escape")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            if self._confirmation is not None:
                self.answer_confirmation(False)

        @bindings.add("y")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            if self._confirmation is not None:
                self.answer_confirmation(True)
            else:
                event.app.current_buffer.insert_text("y")

        @bindings.add("n")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            if self._confirmation is not None:
                self.answer_confirmation(False)
            else:
                event.app.current_buffer.insert_text("n")

        @bindings.add("pageup")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            self.scroll_transcript(20)

        @bindings.add("pagedown")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            self.scroll_transcript(-20)

        @bindings.add("home")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            with self._lock:
                self._follow_bottom = False
                self._scroll_offset = max(0, self._transcript_total_line_count - 1)
            self.invalidate()

        @bindings.add("end")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            with self._lock:
                self._follow_bottom = True
                self._scroll_offset = 0
                self._status.new_output = False
            self.invalidate()

        return bindings


class TuiUI:
    """Adapter that exposes TerminalUI-like methods to the agent loop."""

    def __init__(self, app: TuiApp) -> None:
        self.app = app
        self.console = _TuiConsoleProxy(self)
        self.err_console = _TuiConsoleProxy(self)
        self._active_assistant_item: TranscriptItem | None = None
        self._active_reasoning_item: TranscriptItem | None = None

    def show_system_message(self, text: str) -> None:
        self.app.append_item(
            "system",
            "system",
            render_text("system", text, width=self.app.render_width),
            raw=text,
        )

    def show_startup(
        self,
        *,
        model: str,
        workdir: str,
        agents_loaded: bool,
        tools: Iterable[str],
    ) -> None:
        text = (
            f"model      {model}\n"
            f"workdir    {workdir}\n"
            f"DONG.md    {'loaded' if agents_loaded else 'not found'}\n"
            f"tools      {', '.join(tools)}"
        )
        self.app.append_item("system", "dong", render_text("dong", text, width=self.app.render_width), raw=text)

    def show_repl_help(self, *, skill_count: int) -> None:
        lines = []
        if skill_count:
            lines.append(f"Skills: {skill_count} available  (/skill to list, /skill <name> to load)")
        lines.append("Commands: exit, clear, dir=<path>, /skill, /unskill, /<skill> <prompt>")
        text = "\n".join(lines)
        self.app.append_item("system", "help", render_text("help", text, width=self.app.render_width), raw=text)

    def show_loaded_skill(self, name: str, source: str, *, indent: str = "  ") -> None:
        self.app.append_item(
            "system",
            "skill",
            render_text("skill", f"Loaded skill: {name} ({source})", width=self.app.render_width),
        )

    def show_skill_already_loaded(self, name: str, source: str) -> None:
        self.show_system_message(f"Skill already loaded: {name} ({source})")

    def show_removed_skill(self, name: str) -> None:
        self.show_system_message(f"Removed skill: {name}")

    def show_skill_not_loaded(self, name: str) -> None:
        self.show_warning(f"Skill '{name}' not loaded")

    def show_skill_error(self, error: Exception) -> None:
        self.show_error(str(error))

    def show_unknown_command_or_skill(self, command: str) -> None:
        self.show_warning(f"Unknown command or skill: /{command}. Use /skill to list.")

    def show_context_cleared(self) -> None:
        self.show_system_message("(context cleared)")

    def show_workdir(self, workdir: str) -> None:
        self.show_system_message(f"workdir -> {workdir}")

    def show_input_queued(self, *, pending: int) -> None:
        self.show_system_message(f"queued input ({pending} pending)")

    def show_user_message(self, text: str) -> None:
        self.app.append_item(
            "user",
            "user",
            render_text("user", text, width=self.app.render_width),
            raw=text,
        )

    def confirm_dangerous_command(self, command: str, default: str = "n") -> bool:
        self.app.append_item(
            "warning",
            "danger",
            render_text("Dangerous command", command, width=self.app.render_width),
            raw=command,
        )
        return self.app.request_confirmation(command, default)

    def show_tool_cancelled(self, name: str, args_raw: str) -> None:
        self.app.append_item(
            "tool_result",
            "cancelled",
            render_text("cancelled", f"{name}({args_raw})", width=self.app.render_width),
        )

    def show_tool_result(self, name: str, args_raw: str, result: ToolResult) -> None:
        display_args = _display_args(args_raw)
        status = "ok" if result.success else "failed"
        summary = result.summary or result.error or "(no summary)"
        raw = f"{status} {name}({display_args}) - {summary}"
        if name == "update_plan" and result.detail.strip():
            ansi = render_markdown(f"**{raw}**\n\n{result.detail}", width=self.app.render_width)
            self.app.append_item("plan", "plan", ansi, raw=result.detail)
            return
        if not result.success and (result.error or result.detail):
            detail = (result.error or result.detail).strip()
            raw = f"{raw}\n{detail}"
        self.app.append_item(
            "tool_result",
            status,
            render_text(status, raw, width=self.app.render_width),
            raw=raw,
        )

    def show_working(
        self,
        message: str,
        *,
        timeout_seconds: float | None = None,
        cancel_hint: str = "Ctrl-C 取消当前任务",
    ) -> AbstractContextManager[None]:
        return _TuiWorkingStatus(
            self.app,
            message,
            timeout_seconds=timeout_seconds,
            cancel_hint=cancel_hint,
        )

    def stream_assistant_message(self) -> AbstractContextManager[Callable[[str], None]]:
        return _StreamingTranscriptContext(self.app, self, kind="assistant", title="assistant")

    def stream_reasoning_message(self) -> AbstractContextManager[Callable[[str], None]]:
        return _StreamingTranscriptContext(self.app, self, kind="thinking", title="thinking")

    def show_assistant_message(self, text: str) -> None:
        normalized = _assistant_display_text(text)
        if not normalized:
            return
        item = self._active_assistant_item
        ansi = render_markdown(f"**assistant**\n\n{normalized}", width=self.app.render_width)
        if item is not None:
            self.app.update_item(item, ansi=ansi, raw=normalized)
            self._active_assistant_item = None
        else:
            self.app.append_item("assistant", "assistant", ansi, raw=normalized)

    def show_reasoning_message(self, text: str) -> None:
        normalized = _normalize_model_text(text)
        if not normalized:
            return
        item = self._active_reasoning_item
        ansi = render_markdown(f"**thinking**\n\n{normalized}", width=self.app.render_width)
        if item is not None:
            self.app.update_item(item, ansi=ansi, raw=normalized)
            self._active_reasoning_item = None
        else:
            self.app.append_item("thinking", "thinking", ansi, raw=normalized)

    def show_warning(self, message: str) -> None:
        self.app.append_item(
            "warning",
            "warning",
            render_text("Warning", message, width=self.app.render_width),
            raw=message,
        )

    def show_error(self, message: str) -> None:
        self.app.append_item(
            "error",
            "error",
            render_text("Error", message, width=self.app.render_width),
            raw=message,
        )

    def blank_line(self) -> None:
        self.app.append_item("system", "blank", "\n", raw="")


class _TuiWorkingStatus(AbstractContextManager[None]):
    """Status-bar context manager for long-running TUI work."""

    def __init__(
        self,
        app: TuiApp,
        message: str,
        *,
        timeout_seconds: float | None,
        cancel_hint: str,
    ) -> None:
        self.app = app
        self.message = message
        self.timeout_seconds = timeout_seconds
        self.cancel_hint = cancel_hint
        self.started = 0.0
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self) -> None:
        self.started = time.monotonic()
        if self.message.startswith("正在执行工具："):
            self.app.append_item(
                "tool_start",
                "running",
                render_text("running", self.message, width=self.app.render_width),
                raw=f"running {self.message}",
            )
        self._update()
        self.thread = threading.Thread(target=self._refresh, name="dong-tui-status", daemon=True)
        self.thread.start()
        return None

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=1.0)
        self.app.clear_status()
        return False

    def _refresh(self) -> None:
        while not self.stop.wait(0.5):
            self._update()

    def _update(self) -> None:
        elapsed = int(time.monotonic() - self.started)
        self.app.update_status(_plain_working_message(
            self.message,
            elapsed_seconds=elapsed,
            timeout_seconds=self.timeout_seconds,
            cancel_hint=self.cancel_hint,
        ))


class _StreamingTranscriptContext(AbstractContextManager[Callable[[str], None]]):
    """Streaming transcript item that is updated in place."""

    def __init__(self, app: TuiApp, ui: TuiUI, *, kind: str, title: str) -> None:
        self.app = app
        self.ui = ui
        self.kind = kind
        self.title = title
        self.parts: list[str] = []
        self.item: TranscriptItem | None = None
        self.last_render = 0.0

    def __enter__(self) -> Callable[[str], None]:
        return self.write

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self._render(force=True)
        return False

    def write(self, delta: str) -> None:
        if not delta:
            return
        self.parts.append(delta)
        self._render(force=False)

    def _render(self, *, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self.last_render < 0.05:
            return
        self.last_render = now
        raw = "".join(self.parts)
        if not raw:
            return
        ansi = render_markdown(f"**{self.title}**\n\n{raw}", width=self.app.render_width)
        if self.item is None:
            self.item = self.app.append_item(self.kind, self.title, ansi, raw=raw)
            self.item.streaming = True
            if self.kind == "assistant":
                self.ui._active_assistant_item = self.item
            elif self.kind == "thinking":
                self.ui._active_reasoning_item = self.item
        else:
            self.app.update_item(self.item, ansi=ansi, raw=raw)


def render_markdown(text: str, *, width: int = 100) -> str:
    """Render Rich Markdown offscreen into ANSI text for the prompt_toolkit transcript."""
    output = StringIO()
    console = Console(
        file=output,
        force_terminal=True,
        color_system="truecolor",
        width=width,
        theme=DONG_LIGHT_THEME,
        highlight=False,
    )
    console.print(Markdown(
        text,
        code_theme=MARKDOWN_CODE_THEME,
        inline_code_theme=MARKDOWN_CODE_THEME,
    ))
    return output.getvalue().rstrip()


def render_text(title: str, text: str, *, width: int = 100) -> str:
    """Render styled plain text offscreen into ANSI text."""
    output = StringIO()
    console = Console(
        file=output,
        force_terminal=True,
        color_system="truecolor",
        width=width,
        theme=DONG_LIGHT_THEME,
        highlight=False,
    )
    console.print(Text(title, style="bold #0b5cad"))
    if text:
        console.print(text)
    return output.getvalue().rstrip()


def _display_args(args_raw: str, limit: int = 60) -> str:
    return args_raw[:limit] + ("..." if len(args_raw) > limit else "")


def _fit_to_width(text: str, width: int) -> str:
    """Fit plain status text to a terminal display width."""
    if width <= 1:
        return ""
    if get_cwidth(text) <= width:
        return text
    result = []
    used = 0
    marker = "…"
    marker_width = get_cwidth(marker)
    limit = max(0, width - marker_width)
    for char in text:
        char_width = get_cwidth(char)
        if used + char_width > limit:
            break
        result.append(char)
        used += char_width
    return "".join(result).rstrip() + marker


def _plain_working_message(
    message: str,
    *,
    elapsed_seconds: int,
    timeout_seconds: float | None,
    cancel_hint: str,
) -> str:
    elapsed = max(0, elapsed_seconds)
    if timeout_seconds is None:
        suffix = f"{elapsed}s · {cancel_hint}"
    else:
        timeout_label = f"{timeout_seconds:g}s"
        if elapsed < timeout_seconds:
            suffix = f"{elapsed}s/{timeout_label} · {cancel_hint}"
        else:
            suffix = f"{elapsed}s/{timeout_label} · 已超过超时阈值，等待清理... · {cancel_hint}"
    return f"{message} {suffix}"


def _tool_args_summary(args_raw: str) -> str:
    try:
        parsed = json.loads(args_raw)
    except Exception:
        return _display_args(args_raw)
    if not isinstance(parsed, dict):
        return _display_args(args_raw)
    return _display_args(json.dumps(parsed, ensure_ascii=False))
