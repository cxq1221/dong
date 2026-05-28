"""dong 的 inline TUI：历史输出进入终端 scrollback，底部保留交互区。"""

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

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.completion import Completer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
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
    _bind_control_shortcut,
    _cancel_shortcut_hint,
    _normalize_model_text,
)
from dong.session_recovery import SessionRestoreResult, session_message_text


SYSTEM_SLASH_COMMAND_ORDER = ["/compact", "/sessions", "/ocr", "/skill", "/skills", "/unskill", "/bye"]
SYSTEM_SLASH_COMMANDS = set(SYSTEM_SLASH_COMMAND_ORDER)
COMPLETION_GROUP_SEPARATOR = "----------"
SESSION_PICKER_BATCH_SIZE = 10
SKILL_MENU_COMMANDS = ("/skill", "/skills")
COMPLETION_MENU_MAX_CANDIDATES = 10


@dataclass
class TranscriptItem:
    """Transcript 条目；committed 写入终端历史，streaming 只显示在 live 区。"""

    kind: str
    title: str
    raw: str
    ansi: str
    streaming: bool = False
    committed: bool = False
    created_at: float = field(default_factory=time.monotonic)


@dataclass
class StatusState:
    """Current TUI status bar state."""

    label: str = "idle"
    queued: int = 0
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


@dataclass
class SessionPickerState:
    """`/sessions` 会话选择器状态，只通过键盘在 live 区交互。"""

    items: list[object]
    current_session_id: str | None
    selected_index: int = 0
    visible_count: int = SESSION_PICKER_BATCH_SIZE
    done: threading.Event = field(default_factory=threading.Event)
    result: str | None = None
    live_item: TranscriptItem | None = None


class _LiveTranscriptControl(FormattedTextControl):
    """底部 live viewport，只展示 streaming 和临时选择器，不承载历史滚动。"""

    def __init__(self, app: TuiApp) -> None:
        self.app = app
        super().__init__(
            app._formatted_live_transcript,
        )

    def invalidate_content(self) -> None:
        """清理 prompt_toolkit 的片段缓存，确保 live 内容立即重算。"""
        self.reset()
        self._fragment_cache.clear()
        self._content_cache.clear()


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
    """拥有 inline prompt_toolkit 交互区和 scrollback transcript 提交队列。"""

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
        # TUI 取消事件由主线程设置，worker/LLM 流式读取层轮询它来协作退出。
        self._cancel_event = threading.Event()
        self._worker_errors: list[BaseException] = []
        self._worker_thread = threading.Thread(
            target=self._worker,
            name="dong-tui-worker",
            daemon=False,
        )
        # 终端输出必须串行化，避免后台 worker 和 prompt_toolkit 重绘交错。
        self._terminal_write_lock = threading.Lock()
        self._pending_scrollback_texts: list[str] = []
        self._app_thread_id: int | None = None
        self._running = False
        self._shutdown_requested = False
        self._busy = False
        # committed transcript 是 append-only 历史；live_items 只给底部临时视图。
        self._live_items: list[TranscriptItem] = []
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
        self.live_control = _LiveTranscriptControl(self)
        self.status_control = FormattedTextControl(self._formatted_status)
        self.completion_control = FormattedTextControl(self._formatted_completion_menu)
        self.application = Application(
            layout=Layout(
                HSplit([
                    Window(
                        content=self.live_control,
                        height=6,
                        wrap_lines=True,
                        always_hide_cursor=True,
                    ),
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
            full_screen=False,
            mouse_support=False,
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
        """运行 inline TUI；历史输出保留在普通终端 scrollback。"""
        self._running = True
        self._shutdown_requested = False
        self._app_thread_id = threading.get_ident()
        self._worker_thread.start()
        try:
            self.application.run(pre_run=self._flush_pending_scrollback_texts)
        finally:
            self._running = False
            self._app_thread_id = None
            self.request_exit()
            self._discard_pending_inputs()
            self._input_queue.put(None)
            self._worker_thread.join(timeout=2.0)
            if self._worker_errors:
                raise self._worker_errors[0]

    def print_exit_resume_command(self) -> None:
        """退出交互区前在普通终端区域打印恢复命令。"""
        with self._lock:
            if self._exit_resume_printed or self.resume_command_provider is None:
                return
            self._exit_resume_printed = True
        command = self.resume_command_provider()
        text = f"\r\n  Resume this session:\r\n    {command}\r\n"
        self._write_scrollback_text(text)

    def request_exit(self) -> None:
        """Request TUI shutdown and unblock any synchronous confirmation prompt."""
        with self._lock:
            self._shutdown_requested = True
            self._status.cancellation_requested = True
            self._cancel_event.set()
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
        """追加稳定 transcript 条目，并写入终端原生 scrollback。"""
        item = TranscriptItem(
            kind=kind,
            title=title,
            raw=raw if raw is not None else ansi,
            ansi=ansi,
            committed=True,
        )
        with self._lock:
            self._transcript.append(item)
        self._commit_item_to_scrollback(item)
        self.invalidate()
        return item

    def update_item(self, item: TranscriptItem, *, ansi: str, raw: str | None = None) -> None:
        """更新 live 条目；已提交历史不可修改，差异会作为新块追加。"""
        with self._lock:
            was_committed = item.committed
            item.ansi = ansi
            if raw is not None:
                item.raw = raw
            if not was_committed:
                self.live_control.invalidate_content()
        if was_committed:
            self.append_item(item.kind, item.title, ansi, raw=item.raw)
        self.invalidate()

    def replace_transcript(
        self,
        entries: list[tuple[str, str, str, str]],
    ) -> None:
        """用恢复出的完整上下文替换内存 transcript，并一次性提交到 scrollback。"""
        items = [
            TranscriptItem(kind=kind, title=title, ansi=ansi, raw=raw, committed=True)
            for kind, title, ansi, raw in entries
        ]
        with self._lock:
            self._transcript = items
            self._live_items.clear()
            self.live_control.invalidate_content()
        for item in items:
            self._commit_item_to_scrollback(item)
        self.invalidate()

    def start_live_item(self, kind: str, title: str, ansi: str, raw: str | None = None) -> TranscriptItem:
        """创建底部 live 条目；内容完成前不写入 scrollback。"""
        item = TranscriptItem(
            kind=kind,
            title=title,
            raw=raw if raw is not None else ansi,
            ansi=ansi,
            streaming=True,
            committed=False,
        )
        with self._lock:
            self._live_items.append(item)
            self.live_control.invalidate_content()
        self.invalidate()
        return item

    def commit_live_item(self, item: TranscriptItem) -> None:
        """把 live 条目转成不可变历史块并提交到终端 scrollback。"""
        should_commit = False
        with self._lock:
            if not item.committed:
                item.streaming = False
                item.committed = True
                self._remove_live_item_locked(item)
                self._transcript.append(item)
                should_commit = True
            self.live_control.invalidate_content()
        if should_commit:
            self._commit_item_to_scrollback(item)
        self.invalidate()

    def _remove_live_item_locked(self, item: TranscriptItem) -> None:
        """从 live viewport 移除临时条目；调用方持有锁。"""
        self._live_items = [existing for existing in self._live_items if existing is not item]

    def _commit_item_to_scrollback(self, item: TranscriptItem) -> None:
        """把一个稳定 transcript 块输出到普通终端历史。"""
        if not item.ansi.strip():
            return
        text = f"\r\n{item.ansi.rstrip()}\r\n"
        self._write_scrollback_text(text)

    def _write_scrollback_text(self, text: str) -> None:
        """通过 prompt_toolkit 安全通道写 scrollback，失败时退回 stdout。"""
        if not text:
            return

        if not self._running:
            with self._lock:
                self._pending_scrollback_texts.append(text)
            return

        def write() -> None:
            with self._terminal_write_lock:
                try:
                    self.application.output.write_raw(text)
                    self.application.output.flush()
                except Exception:
                    os.write(1, text.encode("utf-8", errors="replace"))

        loop = getattr(self.application, "loop", None)
        if self._running and loop is not None:
            if threading.get_ident() == self._app_thread_id:
                try:
                    run_in_terminal(write, render_cli_done=False)
                    return
                except Exception:
                    write()
                    return

            done = threading.Event()

            def schedule() -> None:
                try:
                    future = run_in_terminal(write, render_cli_done=False)
                    future.add_done_callback(lambda _future: done.set())
                except Exception:
                    write()
                    done.set()

            try:
                loop.call_soon_threadsafe(schedule)
                done.wait(timeout=2.0)
                return
            except Exception:
                pass
        write()

    def _flush_pending_scrollback_texts(self) -> None:
        """应用启动后刷新 run() 前积累的 startup transcript。"""
        with self._lock:
            pending = self._pending_scrollback_texts
            self._pending_scrollback_texts = []
        for text in pending:
            self._write_scrollback_text(text)

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
        # Session 很多时默认只露出前 10 个；继续向下移动时再逐批展开。
        visible_count = min(SESSION_PICKER_BATCH_SIZE, len(items))
        selected_index = min(selected_index, max(0, visible_count - 1))
        state = SessionPickerState(
            items=items,
            current_session_id=current_session_id,
            selected_index=selected_index,
            visible_count=visible_count,
        )
        with self._lock:
            self._session_picker = state
        state.live_item = self.start_live_item(
            "sessions",
            "sessions",
            self._render_session_picker(state),
            raw=self._raw_session_picker(state),
        )
        state.done.wait()
        with self._lock:
            if self._session_picker is state:
                self._session_picker = None
            result = state.result
            if state.live_item is not None:
                self._remove_live_item_locked(state.live_item)
            self.live_control.invalidate_content()
            if not result:
                self._status.label = "Session selection cancelled"
        self.invalidate()
        return result

    def move_session_picker_selection(self, delta: int) -> bool:
        """移动 session 选择器高亮行。"""
        with self._lock:
            state = self._session_picker
            if state is None:
                return False
            next_index = max(0, min(len(state.items) - 1, state.selected_index + delta))
            if next_index >= state.visible_count and state.visible_count < len(state.items):
                state.visible_count = min(
                    len(state.items),
                    state.visible_count + SESSION_PICKER_BATCH_SIZE,
                )
            state.selected_index = next_index
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

    def _refresh_session_picker(self, state: SessionPickerState) -> None:
        item = state.live_item
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
            self._cancel_event.set()
        self.invalidate()

    def cancel_requested(self) -> bool:
        """返回当前 worker 任务是否已收到用户取消请求。"""
        return self._cancel_event.is_set()

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
            # 菜单选择和 Codex 的 `$` mention popup 一样循环移动，长按方向键不会卡在边界。
            self._completion_selection_index = (
                self._completion_selection_index + delta
            ) % len(candidates)
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
        if selected == "/sessions":
            self.composer.buffer.reset()
            self.submit_text(selected)
            return True
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
        if TuiApp._skill_menu_prefix(current) is not None:
            return f"/skill {selected}"
        if current.startswith("/unskill "):
            return f"/unskill {selected}"
        return selected

    def scroll_transcript(self, lines: int) -> None:
        """历史滚动交给终端 scrollback；保留方法是为了兼容键位和测试入口。"""
        return

    def _record_input_history_locked(self, text: str) -> None:
        """记录用户已提交输入，并结束当前历史浏览状态。"""
        self._input_history.append(text)
        self._input_history_index = None
        self._input_history_draft = ""

    def _worker(self) -> None:
        while True:
            item = self._input_queue.get()
            try:
                if item is None:
                    return
                with self._lock:
                    self._cancel_event.clear()
                    self._busy = True
                    self._status.queued = self._input_queue.qsize()
                    self._status.label = "working"
                    self._status.cancellation_requested = False
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
                    self._cancel_event.clear()
                    self._status.queued = self._input_queue.qsize()
                    if self._status.label == "working":
                        self._status.label = "idle"
                    self._status.cancellation_requested = False
                self._input_queue.task_done()
                self.invalidate()

    def _discard_pending_inputs(self) -> None:
        while True:
            try:
                self._input_queue.get_nowait()
            except Empty:
                return
            self._input_queue.task_done()

    def _formatted_live_transcript(self) -> ANSI:
        """渲染固定高度 live viewport，历史内容不在这里重画。"""
        with self._lock:
            text = "\n\n".join(item.ansi.rstrip() for item in self._live_items if item.ansi.strip())
        if not text:
            text = f"\x1b[36m{self.title}\x1b[0m"
        return ANSI(text)

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
            if self._status.cancellation_requested:
                parts.append("cancel requested")
            if self._confirmation is not None:
                parts.append("confirm y/N")
            parts.append("native copy")
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
        return max(1, len(self._completion_display_rows()))

    def _formatted_completion_menu(self) -> list[tuple[str, str]]:
        """把 slash completion 渲染成竖向列表，贴近 claw-code 的 list completion。"""
        rows = self._completion_display_rows()
        candidates = self._completion_candidates()
        with self._lock:
            selected_index = self._completion_selection_index
        selected_candidate = candidates[selected_index] if candidates else None
        fragments: list[tuple[str, str]] = []
        for row_index, row in enumerate(rows):
            is_separator = row == COMPLETION_GROUP_SEPARATOR
            selected = not is_separator and row == selected_candidate
            prefix = "› " if selected else "  "
            style = "fg:#0b5cad bold" if selected else "fg:#8c959f" if is_separator else "fg:#24292f"
            fragments.append((style, f"  {prefix}{row}"))
            if row_index < len(rows) - 1:
                fragments.append(("", "\n"))
        return fragments

    def _completion_display_rows(self) -> list[str]:
        """返回带系统/skill 分隔线的 completion 展示行。"""
        candidates = self._visible_completion_candidates()
        system = [item for item in candidates if item in SYSTEM_SLASH_COMMANDS]
        skills = [item for item in candidates if item not in SYSTEM_SLASH_COMMANDS]
        if system and skills:
            return [*system, COMPLETION_GROUP_SEPARATOR, *skills]
        return candidates

    def _visible_completion_candidates(self) -> list[str]:
        """只渲染围绕当前选择的小窗口；完整候选集仍可用上下键遍历。"""
        candidates = self._completion_candidates()
        if len(candidates) <= COMPLETION_MENU_MAX_CANDIDATES:
            return candidates
        with self._lock:
            selected_index = self._completion_selection_index
        half_window = COMPLETION_MENU_MAX_CANDIDATES // 2
        start = max(0, selected_index - half_window)
        start = min(start, len(candidates) - COMPLETION_MENU_MAX_CANDIDATES)
        end = start + COMPLETION_MENU_MAX_CANDIDATES
        return candidates[start:end]

    def _completion_candidates(self) -> list[str]:
        """返回当前 slash 输入的候选项，供竖向菜单和测试复用。"""
        text = self.composer.text
        if self._confirmation is not None or not text.startswith("/"):
            return []
        if self._skill_menu_prefix(text) is not None:
            candidates = self._skill_name_completion_candidates(text)
        else:
            completer = _SlashAwareCompleter(self._completion_words())
            candidates = [
                completion.text
                for completion in completer.get_completions(Document(text), None)
            ]
        def sort_key(item: str) -> tuple[int, str]:
            if item in SYSTEM_SLASH_COMMANDS:
                return (0, SYSTEM_SLASH_COMMAND_ORDER.index(item))
            return (1, item)

        candidates = sorted(candidates, key=sort_key)
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

    def _skill_name_completion_candidates(self, text: str) -> list[str]:
        """`/skill` 和 `/skills` 精确输入时直接展示 skill 名，行为对齐 Codex 的 `$` 菜单。"""
        prefix = self._skill_menu_prefix(text) or ""
        names = sorted({
            item.removeprefix("/skill ").strip()
            for item in self._completion_words()
            if item.startswith("/skill ") and item.removeprefix("/skill ").strip()
        })
        if prefix:
            names = [name for name in names if name.lower().startswith(prefix.lower())]
        return names

    @staticmethod
    def _skill_menu_prefix(text: str) -> str | None:
        """返回 `/skill[s]` 菜单后的过滤前缀；None 表示不是 skill 菜单上下文。"""
        for command in SKILL_MENU_COMMANDS:
            if text == command:
                return ""
            command_prefix = f"{command} "
            if text.startswith(command_prefix):
                return text.removeprefix(command_prefix).strip()
        return None

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
        visible_items = state.items[: state.visible_count]
        for index, item in enumerate(visible_items):
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
        if state.visible_count < len(state.items):
            remaining = len(state.items) - state.visible_count
            lines.append(
                "\x1b[2m"
                f"Scroll down to load {min(SESSION_PICKER_BATCH_SIZE, remaining)} more "
                f"({remaining} remaining)"
                "\x1b[0m"
            )
        return "\n".join(lines)

    @staticmethod
    def _raw_session_picker(state: SessionPickerState) -> str:
        """生成不含 ANSI 的 session 选择器文本，供测试和复制使用。"""
        lines = ["Sessions", "Current workspace sessions"]
        for index, item in enumerate(state.items[: state.visible_count]):
            marker = ">" if index == state.selected_index else " "
            current = "*" if getattr(item, "session_id", "") == state.current_session_id else " "
            lines.append(
                f"{marker} {current} {getattr(item, 'session_id', '')} "
                f"{getattr(item, 'message_count', 0)} msgs "
                f"{getattr(item, 'prompt_preview', '')}"
            )
            lines.append(f"      assistant: {getattr(item, 'assistant_preview', '')}")
        if state.visible_count < len(state.items):
            lines.append(f"Scroll down to load more ({len(state.items) - state.visible_count} remaining)")
        return "\n".join(lines)

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

        def insert_newline(event) -> None:  # type: ignore[no-untyped-def]
            event.app.current_buffer.insert_text("\n")

        _bind_control_shortcut(bindings, "j", insert_newline)

        def paste_clipboard_image(event) -> None:  # type: ignore[no-untyped-def]
            self.paste_clipboard_image()

        _bind_control_shortcut(bindings, "v", paste_clipboard_image)

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

        def copy_or_cancel(event) -> None:  # type: ignore[no-untyped-def]
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

        _bind_control_shortcut(bindings, "c", copy_or_cancel, eager=True)

        def exit_app(event) -> None:  # type: ignore[no-untyped-def]
            self.request_exit()
            event.app.exit()

        _bind_control_shortcut(bindings, "d", exit_app, eager=True)

        def insert_last_assistant(event) -> None:  # type: ignore[no-untyped-def]
            if self._confirmation is not None:
                return
            raw = self.get_last_assistant_raw()
            if raw is not None:
                event.app.current_buffer.insert_text(raw)

        _bind_control_shortcut(bindings, "y", insert_last_assistant)

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
            return

        @bindings.add("pagedown")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            return

        @bindings.add("home")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            return

        @bindings.add("end")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            return

        @bindings.add("f2")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            return

        return bindings


class TuiUI:
    """Adapter that exposes TerminalUI-like methods to the agent loop."""

    preserve_working_status_during_streaming = True

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

    def show_session_restored(self, result: SessionRestoreResult) -> None:
        """切换到恢复 session 的 Transcript，而不是展示压缩摘要。"""
        text = f"Restored session: {result.session.session_id}\nContext loaded. Continue typing."
        entries = [
            (
                "system",
                "session",
                render_text("session", text, width=self.app.render_width),
                text,
            )
        ]
        entries.extend(self._restored_session_entries(result.session.messages))
        self.app.replace_transcript(entries)

    def _restored_session_entries(
        self,
        messages: Iterable[object],
    ) -> list[tuple[str, str, str, str]]:
        """把恢复的 session messages 重建成 TUI Transcript 条目。"""
        entries: list[tuple[str, str, str, str]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "")
            if role == "user":
                raw = session_message_text(message.get("content"))
                if raw:
                    entries.append((
                        "user",
                        "user",
                        render_text("user", raw, width=self.app.render_width),
                        raw,
                    ))
                continue
            if role == "assistant":
                reasoning = _normalize_model_text(str(message.get("reasoning_content") or ""))
                if reasoning:
                    entries.append((
                        "thinking",
                        "thinking",
                        render_markdown(f"**thinking**\n\n{reasoning}", width=self.app.render_width),
                        reasoning,
                    ))
                raw = _assistant_display_text(session_message_text(message.get("content")))
                if raw:
                    entries.append((
                        "assistant",
                        "assistant",
                        render_markdown(f"**assistant**\n\n{raw}", width=self.app.render_width),
                        raw,
                    ))
                continue
            if role == "tool":
                raw = session_message_text(message.get("content"))
                if raw:
                    title = str(message.get("name") or "tool")
                    text = f"{title}: {raw}"
                    entries.append((
                        "tool_result",
                        title,
                        render_text(title, text, width=self.app.render_width),
                        text,
                    ))
        return entries

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

    def cancel_requested(self) -> bool:
        """把 TUI 取消事件暴露给 CLI/LLM 调用链。"""
        return self.app.cancel_requested()

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
        cancel_hint: str | None = None,
    ) -> AbstractContextManager[None]:
        resolved_cancel_hint = cancel_hint or _cancel_shortcut_hint()
        return _TuiWorkingStatus(
            self.app,
            message,
            timeout_seconds=timeout_seconds,
            cancel_hint=resolved_cancel_hint,
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
            self._active_assistant_item = None
            if item.raw.strip() == normalized.strip():
                return
            self.app.append_item("assistant_update", "assistant update", ansi, raw=normalized)
        else:
            self.app.append_item("assistant", "assistant", ansi, raw=normalized)

    def show_reasoning_message(self, text: str) -> None:
        normalized = _normalize_model_text(text)
        if not normalized:
            return
        item = self._active_reasoning_item
        ansi = render_markdown(f"**thinking**\n\n{normalized}", width=self.app.render_width)
        if item is not None:
            self._active_reasoning_item = None
            if item.raw.strip() == normalized.strip():
                return
            self.app.append_item("thinking_update", "thinking update", ansi, raw=normalized)
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
            self.app.commit_live_item(self.item)
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
            self.item = self.app.start_live_item(self.kind, self.title, ansi, raw=raw)
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
