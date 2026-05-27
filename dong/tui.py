"""Fullscreen prompt_toolkit TUI for dong interactive mode."""

from __future__ import annotations

import json
import os
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
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from dong.clipboard_image import ClipboardImageError, save_clipboard_image
from dong.ocr import image_marker
from dong.tool import ToolResult
from dong.ui import (
    DONG_LIGHT_THEME,
    MARKDOWN_CODE_THEME,
    REPL_COMMANDS_TEXT,
    SLASH_COMMAND_LINES,
    _SlashAwareCompleter,
    _assistant_display_text,
    _normalize_model_text,
    session_transcript_preview,
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
class ContextUsageState:
    """TUI status bar 中展示的上下文预算使用情况。"""

    estimated_tokens: int = 0
    budget_limit: int = 0
    context_window_tokens: int = 0
    compacted: bool = False


@dataclass
class ConfirmationRequest:
    """A synchronous confirmation owned by the active agent/tool turn."""

    command: str
    default: str
    done: threading.Event = field(default_factory=threading.Event)
    result: bool | None = None


@dataclass(frozen=True)
class ScrollbarState:
    """Transcript 滚动条的可视区和滑块位置。"""

    visible: bool
    track_height: int
    thumb_top: int
    thumb_height: int
    max_scroll_offset: int


@dataclass
class SessionPickerState:
    """`/sessions` 会话选择器状态，供键盘和鼠标事件共享。"""

    items: list[object]
    current_session_id: str | None
    selected_index: int = 0
    done: threading.Event = field(default_factory=threading.Event)
    result: str | None = None
    transcript_item: TranscriptItem | None = None


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

    def create_content(self, width: int, height: int | None):  # type: ignore[no-untyped-def]
        self.app.set_transcript_viewport_height(height or 1)
        return super().create_content(width, height)

    def mouse_handler(self, mouse_event: MouseEvent):  # type: ignore[no-untyped-def]
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            self.app.stop_scrollbar_drag()
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_DOWN and mouse_event.button == MouseButton.LEFT:
            if self.app.handle_session_picker_mouse(mouse_event.position.y):
                return None
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self.app.scroll_transcript(3)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self.app.scroll_transcript(-3)
            return None
        return super().mouse_handler(mouse_event)


class _TranscriptScrollbarControl(FormattedTextControl):
    """Transcript 右侧滚动条 control，支持点击轨道和拖动滑块。"""

    def __init__(self, app: TuiApp) -> None:
        self.app = app
        super().__init__(app._formatted_scrollbar)

    def invalidate_content(self) -> None:
        """清理滚动条渲染缓存，确保高度和滑块位置立即刷新。"""
        self.reset()
        self._fragment_cache.clear()
        self._content_cache.clear()

    def create_content(self, width: int, height: int | None):  # type: ignore[no-untyped-def]
        self.invalidate_content()
        return super().create_content(width, height)

    def mouse_handler(self, mouse_event: MouseEvent):  # type: ignore[no-untyped-def]
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self.app.scroll_transcript(3)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self.app.scroll_transcript(-3)
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_DOWN and mouse_event.button == MouseButton.LEFT:
            self.app.start_scrollbar_drag(mouse_event.position.y)
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_MOVE:
            if mouse_event.button == MouseButton.LEFT:
                self.app.drag_scrollbar(mouse_event.position.y)
            else:
                self.app.stop_scrollbar_drag()
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            self.app.stop_scrollbar_drag()
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


class _TuiExitRequested(Exception):
    """Internal signal used to stop the worker after the TUI is shutting down."""


class TuiApp:
    """Owns the fullscreen prompt_toolkit application and worker queue."""

    def __init__(
        self,
        *,
        process_input: Callable[[str, "TuiUI"], bool],
        completion_provider: Callable[[], Iterable[str]],
        resume_command_provider: Callable[[], str] | None = None,
        title: str = "dong",
        workdir: str | None = None,
    ) -> None:
        self.process_input = process_input
        self.completion_provider = completion_provider
        self.resume_command_provider = resume_command_provider
        self.title = title
        self.workdir = workdir or os.getcwd()
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
        self._shutdown_requested = False
        self._busy = False
        self._follow_bottom = True
        self._scroll_offset = 0
        self._scroll_view_end_line: int | None = None
        self._transcript_total_line_count = 1
        self._transcript_cursor_line = 0
        self._transcript_viewport_height = 1
        self._scrollbar_drag_offset: int | None = None
        # 默认捕获鼠标：滚轮滚动 transcript，拖拽选择文本可切回 copy 模式（F2）。
        self._mouse_capture_enabled = True
        self._last_ctrl_c = 0.0
        self._exit_resume_printed = False
        self._render_width_override: int | None = None
        self._completion_cache: list[str] = []
        self._completion_cache_at = 0.0
        self._completion_selection_text = ""
        self._completion_selection_index = 0
        self._input_history: list[str] = []
        self._input_history_index: int | None = None
        self._input_history_draft = ""
        self._confirmation: ConfirmationRequest | None = None
        self._session_picker: SessionPickerState | None = None
        self._session_picker_result_item: TranscriptItem | None = None
        self._transcript: list[TranscriptItem] = []
        self._status = StatusState()
        self._context_usage: ContextUsageState | None = None

        self.composer = TextArea(
            multiline=True,
            completer=_DynamicSlashCompleter(self._completion_words),
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
        self.scrollbar_control = _TranscriptScrollbarControl(self)
        self.status_control = FormattedTextControl(self._formatted_status)
        self.completion_control = FormattedTextControl(self._formatted_completion_menu)
        self.application = Application(
            layout=Layout(
                HSplit([
                    VSplit([
                        Window(
                            content=self.transcript_control,
                            wrap_lines=True,
                            always_hide_cursor=True,
                        ),
                        Window(
                            content=self.scrollbar_control,
                            width=1,
                            always_hide_cursor=True,
                        ),
                    ]),
                    Window(
                        content=self.status_control,
                        height=1,
                        always_hide_cursor=True,
                        style="reverse",
                    ),
                    ConditionalContainer(
                        Window(
                            content=self.completion_control,
                            height=self._completion_menu_height,
                            always_hide_cursor=True,
                            style="class:completion-menu",
                        ),
                        filter=Condition(self._has_completion_menu),
                    ),
                    self.composer,
                ]),
                focused_element=self.composer,
            ),
            key_bindings=self.key_bindings,
            full_screen=True,
            mouse_support=Condition(
                lambda: self._mouse_capture_enabled or self._session_picker is not None
            ),
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
        return max(20, columns - 3)

    def run(self) -> None:
        """Run the fullscreen TUI until the user exits."""
        self._running = True
        self._shutdown_requested = False
        self._worker_thread.start()
        try:
            self.application.run()
        finally:
            self._running = False
            self.request_exit()
            self._discard_pending_inputs()
            self._input_queue.put(None)
            self._worker_thread.join(timeout=2.0)
            if self._worker_errors:
                raise self._worker_errors[0]

    def print_exit_resume_command(self) -> None:
        """退出全屏界面前在普通终端区域打印恢复命令。"""
        with self._lock:
            if self._exit_resume_printed or self.resume_command_provider is None:
                return
            self._exit_resume_printed = True
        command = self.resume_command_provider()
        text = f"\r\n  Resume this session:\r\n    {command}\r\n"
        try:
            self.application.output.write_raw(text)
            self.application.output.flush()
        except Exception:
            os.write(1, text.encode("utf-8"))

    def request_exit(self) -> None:
        """Request TUI shutdown and unblock any synchronous confirmation prompt."""
        with self._lock:
            self._shutdown_requested = True
            self._status.cancellation_requested = True
            request = self._confirmation
            if request is not None and not request.done.is_set():
                request.result = False
                request.done.set()
        self.invalidate()

    def submit_text(self, text: str) -> None:
        """Submit composer text to the worker queue."""
        text = text.strip()
        if not text:
            return
        with self._lock:
            self._record_input_history_locked(text)
            self._input_queue.put(text)
            self._status.queued = self._input_queue.qsize()
            self._status.new_output = False
            self._follow_bottom = True
            self._scroll_view_end_line = None
            self._scroll_offset = 0
            self.transcript_control.invalidate_content()
            self.scrollbar_control.invalidate_content()
        self.invalidate()

    def paste_clipboard_image(self) -> None:
        """把系统剪贴板图片保存为文件，并在输入框插入图片占位符。"""
        if self._confirmation is not None:
            return
        try:
            path = save_clipboard_image(self.workdir)
        except ClipboardImageError as exc:
            self.ui.show_system_message(str(exc))
            return
        marker = f"{image_marker(path)} "
        document = self.composer.buffer.document
        text = document.text_before_cursor + marker + document.text_after_cursor
        self.composer.buffer.document = Document(
            text,
            cursor_position=len(document.text_before_cursor) + len(marker),
        )
        self.ui.show_system_message(f"attached image: {path}")

    def append_item(self, kind: str, title: str, ansi: str, raw: str | None = None) -> TranscriptItem:
        """Append a rendered item to the transcript."""
        item = TranscriptItem(kind=kind, title=title, raw=raw if raw is not None else ansi, ansi=ansi)
        with self._lock:
            self._transcript.append(item)
            if not self._follow_bottom:
                self._status.new_output = True
            self.transcript_control.invalidate_content()
            self.scrollbar_control.invalidate_content()
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
            self.scrollbar_control.invalidate_content()
        self.invalidate()

    def update_status(self, label: str) -> None:
        """Update the status bar text."""
        with self._lock:
            self._status.label = label
        self.invalidate()

    def update_context_usage(
        self,
        *,
        estimated_tokens: int,
        budget_limit: int,
        context_window_tokens: int | None,
        compacted: bool,
    ) -> None:
        """更新 TUI status bar 中的上下文预算使用量。"""
        with self._lock:
            self._context_usage = ContextUsageState(
                estimated_tokens=max(0, estimated_tokens),
                budget_limit=max(0, budget_limit),
                context_window_tokens=max(0, context_window_tokens or 0),
                compacted=compacted,
            )
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
            if self._shutdown_requested:
                raise _TuiExitRequested()
            self._confirmation = request
            self._status.label = f"Dangerous command: {command}  Run command? [y/N]"
        self.invalidate()
        request.done.wait()
        with self._lock:
            self._confirmation = None
            self._status.label = "idle" if not self._busy else "working"
            if self._shutdown_requested:
                raise _TuiExitRequested()
        self.invalidate()
        return bool(request.result)

    def select_session(
        self,
        items: list[object],
        *,
        current_session_id: str | None = None,
    ) -> str | None:
        """打开 session 选择器并等待用户选择或取消。"""
        if not items:
            self.ui.show_system_message("(no sessions for current workspace)")
            return None
        selected_index = 0
        for index, item in enumerate(items):
            if getattr(item, "session_id", None) == current_session_id:
                selected_index = index
                break
        state = SessionPickerState(
            items=items,
            current_session_id=current_session_id,
            selected_index=selected_index,
        )
        with self._lock:
            self._session_picker = state
            self._session_picker_result_item = None
            self._follow_bottom = True
            self._scroll_view_end_line = None
            self._scroll_offset = 0
        state.transcript_item = self.append_item(
            "sessions",
            "sessions",
            self._render_session_picker(state),
            raw=self._raw_session_picker(state),
        )
        state.done.wait()
        with self._lock:
            if self._session_picker is state:
                self._session_picker = None
            item = state.transcript_item
            result = state.result
            if result:
                self._session_picker_result_item = item
            self.transcript_control.invalidate_content()
            self.scrollbar_control.invalidate_content()
        if item is not None and not result:
            text = "Session selection cancelled"
            self.update_item(
                item,
                ansi=render_text("sessions", text, width=self.render_width),
                raw=text,
            )
        self.invalidate()
        return state.result

    def replace_selected_session_picker(self, *, text: str) -> bool:
        """恢复成功后把 session 选择器原地替换成结果提示。"""
        with self._lock:
            item = self._session_picker_result_item
            self._session_picker_result_item = None
        if item is None:
            return False
        self.update_item(
            item,
            ansi=render_text("session", text, width=self.render_width),
            raw=text,
        )
        return True

    def move_session_picker_selection(self, delta: int) -> bool:
        """移动 session 选择器高亮行。"""
        with self._lock:
            state = self._session_picker
            if state is None:
                return False
            state.selected_index = max(
                0,
                min(len(state.items) - 1, state.selected_index + delta),
            )
        self._refresh_session_picker(state)
        return True

    def accept_session_picker(self) -> bool:
        """确认 session 选择器当前高亮项。"""
        with self._lock:
            state = self._session_picker
            if state is None:
                return False
            selected = state.items[state.selected_index]
            state.result = str(getattr(selected, "session_id", ""))
            state.done.set()
        return True

    def cancel_session_picker(self) -> bool:
        """取消 session 选择器。"""
        with self._lock:
            state = self._session_picker
            if state is None:
                return False
            state.result = None
            state.done.set()
        return True

    def handle_session_picker_mouse(self, row: int) -> bool:
        """处理 session 选择器里的鼠标点击；点击某行即恢复该 session。"""
        with self._lock:
            state = self._session_picker
            if state is None:
                return False
            picker_lines = self._render_session_picker(state).splitlines()
            first_row = max(0, self._transcript_viewport_height - len(picker_lines))
            local_row = row - first_row
            index = (local_row - 2) // 2
            if index < 0 or index >= len(state.items):
                return False
            state.selected_index = index
        self._refresh_session_picker(state)
        self.accept_session_picker()
        return True

    def _refresh_session_picker(self, state: SessionPickerState) -> None:
        item = state.transcript_item
        if item is None:
            return
        self.update_item(
            item,
            ansi=self._render_session_picker(state),
            raw=self._raw_session_picker(state),
        )

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

    def navigate_input_history(self, direction: int) -> None:
        """按方向键切换已提交的用户输入；direction < 0 更旧，> 0 更新。"""
        if direction == 0:
            return

        with self._lock:
            if self._confirmation is not None or not self._input_history:
                return

            if self._input_history_index is None:
                if direction > 0:
                    return
                self._input_history_draft = self.composer.text
                next_index = len(self._input_history) - 1
            else:
                next_index = self._input_history_index + direction

            if next_index < 0:
                next_index = 0

            if next_index >= len(self._input_history):
                text = self._input_history_draft
                self._input_history_index = None
            else:
                text = self._input_history[next_index]
                self._input_history_index = next_index

        self.composer.buffer.set_document(
            Document(text, cursor_position=len(text)),
            bypass_readonly=True,
        )
        self.invalidate()

    def move_completion_selection(self, delta: int) -> bool:
        """移动 slash completion 菜单高亮项。"""
        candidates = self._completion_candidates()
        if not candidates:
            return False
        with self._lock:
            self._completion_selection_index = max(
                0,
                min(len(candidates) - 1, self._completion_selection_index + delta),
            )
        self.invalidate()
        return True

    def accept_completion_selection(self) -> bool:
        """把当前高亮 slash completion 填入输入框；已完整输入时交给 Enter 提交。"""
        candidates = self._completion_candidates()
        if not candidates:
            return False
        with self._lock:
            selected = candidates[min(self._completion_selection_index, len(candidates) - 1)]
        current = self.composer.text
        replacement = self._completion_replacement(current, selected)
        if replacement == current:
            return False
        self.composer.buffer.set_document(
            Document(replacement, cursor_position=len(replacement)),
            bypass_readonly=True,
        )
        self.invalidate()
        return True

    @staticmethod
    def _completion_replacement(current: str, selected: str) -> str:
        """根据当前 slash 子命令生成候选项写回输入框的文本。"""
        if current.startswith("/skill "):
            return f"/skill {selected}"
        if current.startswith("/unskill "):
            return f"/unskill {selected}"
        return selected

    def scroll_transcript(self, lines: int) -> None:
        """Scroll transcript history; positive lines move to older output."""
        if lines == 0:
            return
        with self._lock:
            view_end = self._current_view_end_locked()
            self._set_manual_view_end_locked(view_end - lines)
            self.transcript_control.invalidate_content()
            self.scrollbar_control.invalidate_content()
        self.invalidate()

    def set_transcript_viewport_height(self, height: int) -> None:
        """记录 transcript 可视高度，供滚动条和滚动边界计算使用。"""
        with self._lock:
            self._transcript_viewport_height = max(1, height)
            if self._follow_bottom:
                self._scroll_offset = 0
                self._scroll_view_end_line = None
            else:
                self._set_manual_view_end_locked(self._current_view_end_locked())
            if self._max_scroll_offset_locked() == 0:
                self._follow_bottom = True
                self._scroll_offset = 0
                self._scroll_view_end_line = None

    def start_scrollbar_drag(self, row: int) -> None:
        """处理滚动条鼠标按下：滑块内开始拖动，轨道上直接跳转。"""
        with self._lock:
            state = self._scrollbar_state_locked()
            if not state.visible:
                return
            if state.thumb_top <= row < state.thumb_top + state.thumb_height:
                self._scrollbar_drag_offset = row - state.thumb_top
            else:
                self._scrollbar_drag_offset = state.thumb_height // 2
                self._scrollbar_scroll_to_row_locked(row)
            self.transcript_control.invalidate_content()
            self.scrollbar_control.invalidate_content()
        self.invalidate()

    def drag_scrollbar(self, row: int) -> None:
        """拖动滚动条滑块并实时更新 transcript 视图。"""
        with self._lock:
            if self._scrollbar_drag_offset is None:
                return
            self._scrollbar_scroll_to_row_locked(row)
            self.transcript_control.invalidate_content()
            self.scrollbar_control.invalidate_content()
        self.invalidate()

    def stop_scrollbar_drag(self) -> None:
        """结束滚动条拖动状态。"""
        with self._lock:
            self._scrollbar_drag_offset = None
        self.invalidate()

    def _record_input_history_locked(self, text: str) -> None:
        """记录用户已提交输入，并结束当前历史浏览状态。"""
        self._input_history.append(text)
        self._input_history_index = None
        self._input_history_draft = ""

    def toggle_mouse_capture(self) -> bool:
        """切换鼠标捕获；关闭时终端可以直接拖选 transcript 文本复制。"""
        with self._lock:
            self._mouse_capture_enabled = not self._mouse_capture_enabled
            enabled = self._mouse_capture_enabled
            self._scrollbar_drag_offset = None
        self.invalidate()
        return enabled

    def _max_scroll_offset_locked(self) -> int:
        return max(0, self._transcript_total_line_count - self._transcript_viewport_height)

    def _current_view_end_locked(self) -> int:
        """返回当前 transcript 视口底部对应的绝对行号。"""
        total = max(1, self._transcript_total_line_count)
        if self._follow_bottom:
            return total
        if self._scroll_view_end_line is not None:
            return max(1, min(total, self._scroll_view_end_line))
        return max(1, min(total, total - min(self._scroll_offset, self._max_scroll_offset_locked())))

    def _set_manual_view_end_locked(self, end_line: int) -> None:
        """设置手动滚动视口底部行；到底部时恢复自动跟随。"""
        total = max(1, self._transcript_total_line_count)
        viewport_height = max(1, self._transcript_viewport_height)
        if total <= viewport_height or end_line >= total:
            self._follow_bottom = True
            self._scroll_view_end_line = None
            self._scroll_offset = 0
            self._status.new_output = False
            return

        clamped_end = max(viewport_height, min(total, end_line))
        self._follow_bottom = False
        self._scroll_view_end_line = clamped_end
        self._scroll_offset = max(0, total - clamped_end)

    def _scrollbar_state_locked(self) -> ScrollbarState:
        track_height = max(1, self._transcript_viewport_height)
        max_offset = self._max_scroll_offset_locked()
        if max_offset <= 0:
            return ScrollbarState(False, track_height, 0, track_height, 0)
        visible_ratio = self._transcript_viewport_height / self._transcript_total_line_count
        thumb_height = max(1, min(track_height, int(track_height * visible_ratio)))
        max_thumb_top = max(0, track_height - thumb_height)
        view_end = self._current_view_end_locked()
        top_line = max(0, min(max_offset, view_end - self._transcript_viewport_height))
        thumb_top = 0 if max_offset == 0 else round(max_thumb_top * top_line / max_offset)
        return ScrollbarState(True, track_height, thumb_top, thumb_height, max_offset)

    def _scrollbar_scroll_to_row_locked(self, row: int) -> None:
        state = self._scrollbar_state_locked()
        if not state.visible:
            self._follow_bottom = True
            self._scroll_offset = 0
            return
        max_thumb_top = max(0, state.track_height - state.thumb_height)
        desired_top = max(0, min(max_thumb_top, row - (self._scrollbar_drag_offset or 0)))
        if max_thumb_top == 0:
            top_line = 0
        else:
            top_line = round(state.max_scroll_offset * desired_top / max_thumb_top)
        self._set_manual_view_end_locked(top_line + self._transcript_viewport_height)

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
            except _TuiExitRequested:
                try:
                    self.application.exit()
                except Exception:
                    pass
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

    def _formatted_scrollbar(self) -> list[tuple[str, str]]:
        """渲染 light 风格的 1 列 transcript 滚动条。"""
        with self._lock:
            state = self._scrollbar_state_locked()
        if not state.visible:
            fragments: list[tuple[str, str]] = []
            for row in range(state.track_height):
                fragments.append(("", " "))
                if row < state.track_height - 1:
                    fragments.append(("", "\n"))
            return fragments

        fragments = []
        for row in range(state.track_height):
            in_thumb = state.thumb_top <= row < state.thumb_top + state.thumb_height
            if in_thumb:
                fragments.append(("fg:#6b7280", "█"))
            else:
                fragments.append(("fg:#d1d5db", "│"))
            if row < state.track_height - 1:
                fragments.append(("", "\n"))
        return fragments

    def _transcript_cursor_position(self) -> Point:
        """Keep prompt_toolkit's window anchored to the bottom of the current transcript slice."""
        with self._lock:
            return Point(x=0, y=max(0, self._transcript_cursor_line))

    def _formatted_status(self) -> ANSI:
        with self._lock:
            parts = [self._status.label]
            context_usage = self._formatted_context_usage_locked()
            if context_usage:
                parts.append(context_usage)
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
            parts.append("mouse" if self._mouse_capture_enabled else "copy")
        return ANSI("  " + _fit_to_width(" · ".join(parts), self.render_width))

    def _formatted_context_usage_locked(self) -> str:
        """把上下文 token 预算压缩成 status bar 的短文本。"""
        usage = self._context_usage
        if usage is None or usage.budget_limit <= 0:
            return ""
        denominator = usage.context_window_tokens or usage.budget_limit
        percent = min(999, round(usage.estimated_tokens * 100 / denominator))
        compact_at = ""
        if usage.context_window_tokens and usage.budget_limit < usage.context_window_tokens:
            compact_at = f" · compact at {_format_token_count(usage.budget_limit)}"
        compacted = " compacted" if usage.compacted else ""
        return (
            f"ctx {_format_token_count(usage.estimated_tokens)}/"
            f"{_format_token_count(denominator)} {percent}%"
            f"{compact_at}{compacted}"
        )

    def _has_completion_menu(self) -> bool:
        """当前输入需要展示 slash 候选列表。"""
        return bool(self._completion_candidates())

    def _completion_menu_height(self) -> int:
        """候选列表按候选数量占高，避免空白区域挤压 transcript。"""
        return max(1, len(self._completion_candidates()))

    def _formatted_completion_menu(self) -> list[tuple[str, str]]:
        """把 slash completion 渲染成竖向列表，贴近 claw-code 的 list completion。"""
        candidates = self._completion_candidates()
        with self._lock:
            selected_index = min(self._completion_selection_index, max(0, len(candidates) - 1))
        fragments: list[tuple[str, str]] = []
        for index, candidate in enumerate(candidates):
            selected = index == selected_index
            prefix = "› " if selected else "  "
            style = "fg:#0b5cad bold" if selected else "fg:#24292f"
            fragments.append((style, f"  {prefix}{candidate}"))
            if index < len(candidates) - 1:
                fragments.append(("", "\n"))
        return fragments

    def _completion_candidates(self) -> list[str]:
        """返回当前 slash 输入的候选项，供竖向菜单和测试复用。"""
        text = self.composer.text
        if self._confirmation is not None or not text.startswith("/"):
            return []
        completer = _SlashAwareCompleter(self._completion_words())
        candidates = [
            completion.text
            for completion in completer.get_completions(Document(text), None)
        ]
        core_commands = {"/bye", "/compact", "/sessions", "/skill", "/skills", "/unskill"}

        def sort_key(item: str) -> tuple[int, str]:
            if item == "/skill":
                return (0, item)
            if item not in core_commands:
                return (1, item)
            return (2, item)

        candidates = sorted(candidates, key=sort_key)[:10]
        with self._lock:
            if text != self._completion_selection_text:
                self._completion_selection_text = text
                self._completion_selection_index = 0
            if candidates:
                self._completion_selection_index = min(
                    self._completion_selection_index,
                    len(candidates) - 1,
                )
            else:
                self._completion_selection_index = 0
        return candidates

    def _completion_words(self) -> list[str]:
        """Return completion words with a short cache to avoid redraw-time filesystem scans."""
        now = time.monotonic()
        with self._lock:
            if self._completion_cache and now - self._completion_cache_at < 1.0:
                return list(self._completion_cache)
        try:
            words = sorted({item for item in self.completion_provider() if item})
        except Exception:
            with self._lock:
                return list(self._completion_cache)
        with self._lock:
            self._completion_cache = words
            self._completion_cache_at = now
        return list(words)

    def _render_session_picker(self, state: SessionPickerState) -> str:
        """渲染 session 选择器，当前行用反色高亮。"""
        width = self.render_width
        lines = [
            "\x1b[1;36mSessions\x1b[0m  ↑/↓ select · Enter/click resume · Esc cancel",
            "\x1b[2mCurrent workspace sessions\x1b[0m",
        ]
        for index, item in enumerate(state.items):
            selected = index == state.selected_index
            current = "*" if getattr(item, "session_id", "") == state.current_session_id else " "
            session_id = str(getattr(item, "session_id", ""))
            messages = getattr(item, "message_count", 0)
            updated_at_ms = int(getattr(item, "updated_at_ms", 0) or 0)
            updated = time.strftime(
                "%Y-%m-%d %H:%M",
                time.localtime(updated_at_ms / 1000),
            )
            prompt = str(getattr(item, "prompt_preview", ""))
            assistant = str(getattr(item, "assistant_preview", ""))
            first = _fit_to_width(
                f"{'>' if selected else ' '} {current} {session_id}  {messages} msgs  {updated}  {prompt}",
                width,
            )
            second = _fit_to_width(f"      assistant: {assistant}", width)
            if selected:
                first = f"\x1b[7m{first}\x1b[0m"
                second = f"\x1b[7m{second}\x1b[0m"
            lines.extend([first, second])
        return "\n".join(lines)

    @staticmethod
    def _raw_session_picker(state: SessionPickerState) -> str:
        """生成不含 ANSI 的 session 选择器文本，供测试和复制使用。"""
        lines = ["Sessions", "Current workspace sessions"]
        for index, item in enumerate(state.items):
            marker = ">" if index == state.selected_index else " "
            current = "*" if getattr(item, "session_id", "") == state.current_session_id else " "
            lines.append(
                f"{marker} {current} {getattr(item, 'session_id', '')} "
                f"{getattr(item, 'message_count', 0)} msgs "
                f"{getattr(item, 'prompt_preview', '')}"
            )
            lines.append(f"      assistant: {getattr(item, 'assistant_preview', '')}")
        return "\n".join(lines)

    def _render_transcript_lines(self) -> list[str]:
        """按当前滚动状态生成 transcript 可视切片，避免 prompt_toolkit 再次滚动。"""
        text = "\n\n".join(item.ansi.rstrip() for item in self._transcript if item.ansi.strip())
        lines = text.splitlines()
        self._transcript_total_line_count = max(1, len(lines))
        viewport_height = max(1, self._transcript_viewport_height)
        max_offset = self._max_scroll_offset_locked()
        if max_offset == 0:
            self._follow_bottom = True
            self._scroll_offset = 0
            self._scroll_view_end_line = None
        if self._follow_bottom:
            self._scroll_offset = 0
            self._scroll_view_end_line = None
            end = len(lines)
            return lines[max(0, end - viewport_height):end]

        end = self._current_view_end_locked()
        if end >= len(lines):
            self._follow_bottom = True
            self._scroll_offset = 0
            self._scroll_view_end_line = None
            self._status.new_output = False
            end = len(lines)
            return lines[max(0, end - viewport_height):end]

        self._scroll_view_end_line = end
        self._scroll_offset = max(0, self._transcript_total_line_count - end)
        return lines[max(0, end - viewport_height):end]

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("enter")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            if self.accept_session_picker():
                return
            if self.accept_completion_selection():
                return
            if self._confirmation is not None:
                self.answer_confirmation(False)
                return
            text = self.composer.text
            self.composer.buffer.reset()
            self.submit_text(text)

        @bindings.add("c-j")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            event.app.current_buffer.insert_text("\n")

        @bindings.add("c-v")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            self.paste_clipboard_image()

        @bindings.add("up")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            if self.move_session_picker_selection(-1):
                return
            if self.move_completion_selection(-1):
                return
            if event.current_buffer.document.cursor_position_row > 0:
                event.current_buffer.cursor_up()
                return
            self.navigate_input_history(-1)

        @bindings.add("down")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            if self.move_session_picker_selection(1):
                return
            if self.move_completion_selection(1):
                return
            document = event.current_buffer.document
            if document.cursor_position_row < document.line_count - 1:
                event.current_buffer.cursor_down()
                return
            self.navigate_input_history(1)

        @bindings.add("/")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            if self._confirmation is not None:
                return
            event.app.current_buffer.insert_text("/")
            if event.app.current_buffer.document.text_before_cursor == "/":
                event.app.current_buffer.start_completion(select_first=False)

        @bindings.add("c-c", eager=True)
        def _(event) -> None:  # type: ignore[no-untyped-def]
            if self.cancel_session_picker():
                return
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
            self.request_exit()
            event.app.exit()

        @bindings.add("c-d", eager=True)
        def _(event) -> None:  # type: ignore[no-untyped-def]
            self.request_exit()
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
            if self.cancel_session_picker():
                return
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
                self._set_manual_view_end_locked(self._transcript_viewport_height)
                self.transcript_control.invalidate_content()
                self.scrollbar_control.invalidate_content()
            self.invalidate()

        @bindings.add("end")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            with self._lock:
                self._follow_bottom = True
                self._scroll_offset = 0
                self._scroll_view_end_line = None
                self._status.new_output = False
                self.transcript_control.invalidate_content()
                self.scrollbar_control.invalidate_content()
            self.invalidate()

        @bindings.add("f2")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            self.toggle_mouse_capture()

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
        lines.append(REPL_COMMANDS_TEXT)
        text = "\n".join(lines)
        self.app.append_item("system", "help", render_text("help", text, width=self.app.render_width), raw=text)

    def show_slash_commands(self) -> None:
        """展示用户可直接输入的 slash 命令。"""
        text = "\n".join(SLASH_COMMAND_LINES)
        self.app.append_item(
            "system",
            "help",
            render_text("help", text, width=self.app.render_width),
            raw=text,
        )

    def show_session_summaries(
        self,
        summaries,
        *,
        current_session_id: str | None = None,
    ) -> None:
        """展示当前工作区历史 session 列表。"""
        rows = list(summaries)
        if not rows:
            self.show_system_message("(no sessions for current workspace)")
            return

        lines = ["session                 messages  updated              current"]
        for item in rows:
            session_id = str(getattr(item, "session_id", ""))
            updated_at_ms = int(getattr(item, "updated_at_ms", 0) or 0)
            updated = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(updated_at_ms / 1000),
            )
            current = "*" if session_id == current_session_id else ""
            lines.append(
                f"{session_id:<23} {getattr(item, 'message_count', 0):>8}  {updated}  {current}"
            )
        text = "\n".join(lines)
        self.app.append_item(
            "system",
            "sessions",
            render_text("sessions", text, width=self.app.render_width),
            raw=text,
        )

    def select_session(
        self,
        summaries,
        *,
        current_session_id: str | None = None,
    ) -> str | None:
        """打开可交互 session 选择器，并返回选中的 session id。"""
        return self.app.select_session(
            list(summaries),
            current_session_id=current_session_id,
        )

    def show_session_resume_command(self, command: str) -> None:
        """退出 REPL 时展示可复制的恢复命令。"""
        text = f"Resume this session:\n  {command}"
        self.app.append_item(
            "system",
            "session",
            render_text("session", text, width=self.app.render_width),
            raw=text,
        )

    def show_session_restored(
        self,
        session_id: str,
        resume_command: str,
        messages: Iterable[object] = (),
    ) -> None:
        """提示 TUI 已切换到指定 session。"""
        text = f"Restored session: {session_id}\nContext loaded. Continue typing."
        preview = session_transcript_preview(messages)
        if preview:
            text = f"{text}\n\n{preview}"
        if self.app.replace_selected_session_picker(text=text):
            return
        self.app.append_item(
            "system",
            "session",
            render_text("session", text, width=self.app.render_width),
            raw=text,
        )

    def show_loaded_skill(self, name: str, source: str, *, indent: str = "  ") -> None:
        self.app.append_item(
            "system",
            "skill",
            render_text("skill", f"Loaded skill: {name} ({source})", width=self.app.render_width),
        )

    def show_auto_skill(self, name: str, reason: str) -> None:
        """展示本轮自动选择的 skill，保持 TUI 与普通终端 UI 接口一致。"""
        self.show_skill_match(name, mode="auto", reason=reason)

    def show_skill_match(
        self,
        name: str,
        *,
        mode: str,
        source: str | None = None,
        reason: str | None = None,
    ) -> None:
        """展示本轮实际命中的 skill，让 TUI transcript 明确标记上下文选择。"""
        lines = [f"Matched skill: {name}", f"mode      {mode}"]
        if source:
            lines.append(f"source    {source}")
        if reason:
            lines.append(f"reason    {reason}")
        text = "\n".join(lines)
        self.app.append_item(
            "system",
            "skill",
            render_text("skill", text, width=self.app.render_width),
            raw=text,
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
        self.show_warning(f"Unknown command or skill: /{command}. Use / to list commands.")

    def show_context_cleared(self) -> None:
        self.show_system_message("(context cleared)")
        self.app.update_context_usage(
            estimated_tokens=0,
            budget_limit=0,
            context_window_tokens=None,
            compacted=False,
        )

    def show_context_usage(
        self,
        *,
        estimated_tokens: int,
        budget_limit: int,
        context_window_tokens: int | None = None,
        compacted: bool,
    ) -> None:
        """把本轮上下文预算使用量写入 TUI status bar。"""
        self.app.update_context_usage(
            estimated_tokens=estimated_tokens,
            budget_limit=budget_limit,
            context_window_tokens=context_window_tokens,
            compacted=compacted,
        )

    def show_context_compacted(
        self,
        *,
        compacted: bool,
        removed_messages: int,
        preserved_messages: int,
        summary_ref: str | None,
    ) -> None:
        """展示手动压缩结果。"""
        if not compacted:
            self.show_system_message("(not enough old context to compact)")
            return
        suffix = f"\nsummary   {summary_ref}" if summary_ref else ""
        self.show_system_message(
            "context compacted\n"
            f"removed   {removed_messages}\n"
            f"preserved {preserved_messages}"
            f"{suffix}"
        )

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
        self._finish_active_streaming_items()
        self.app.append_item(
            "tool_result",
            "cancelled",
            render_text("cancelled", f"{name}({args_raw})", width=self.app.render_width),
        )

    def show_tool_result(self, name: str, args_raw: str, result: ToolResult) -> None:
        self._finish_active_streaming_items()
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

    def _finish_active_streaming_items(self) -> None:
        """结束当前 streaming 标记，避免下一轮最终答复覆盖已有 transcript。"""
        if self._active_assistant_item is not None:
            self._active_assistant_item.streaming = False
            self._active_assistant_item = None
        if self._active_reasoning_item is not None:
            self._active_reasoning_item.streaming = False
            self._active_reasoning_item = None


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
        if self.item is not None:
            self.item.streaming = False
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
        console.print(Text(text))
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


def _format_token_count(value: int) -> str:
    """把 token 数压成 status bar 友好的短格式。"""
    value = max(0, value)
    if value >= 1_000_000:
        rendered = f"{value / 1_000_000:.1f}M"
    elif value >= 1_000:
        rendered = f"{value / 1_000:.1f}k"
    else:
        rendered = str(value)
    return rendered.replace(".0", "")


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
