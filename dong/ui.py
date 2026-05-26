"""dong 的终端 UI 适配层，集中封装 rich 和 prompt_toolkit 输出。"""

from __future__ import annotations

import sys
import textwrap
import json
import time
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from threading import Event, Thread
from typing import TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from dong.tool import ToolResult


DONG_LIGHT_THEME = Theme({
    "markdown.paragraph": "#24292f",
    "markdown.h1": "bold #0b5cad",
    "markdown.h2": "bold #0b5cad",
    "markdown.h3": "bold #0b5cad",
    "markdown.h4": "bold #0b5cad",
    "markdown.h5": "bold #0b5cad",
    "markdown.h6": "bold #0b5cad",
    "markdown.strong": "bold #24292f",
    "markdown.em": "italic #24292f",
    "markdown.code": "#005a9e",
    "markdown.code_block": "#24292f",
    "markdown.item": "#24292f",
    "markdown.item.bullet": "#6a737d",
    "markdown.item.number": "#6a737d",
    "markdown.link": "underline #0969da",
    "markdown.link_url": "#0969da",
    "markdown.block_quote": "#57606a",
    "markdown.hr": "#d0d7de",
    "markdown.table.border": "#d0d7de",
    "markdown.table.header": "bold #24292f",
})

MARKDOWN_CODE_THEME = "ansi_light"


class _SlashAwareCompleter(Completer):
    """自定义补全器：输入 / 开头时只显示 slash 命令和 skill，不混入普通单词。"""

    def __init__(self, words: list[str]) -> None:
        """按是否以 / 开头、是否含空格分成三组。"""
        self._slash_words: list[str] = []
        self._skill_names: list[str] = []
        self._loaded_skill_names: list[str] = []
        self._normal_words: list[str] = []

        for w in words:
            if w.startswith("/skill "):
                self._skill_names.append(w.removeprefix("/skill ").strip())
            elif w.startswith("/unskill "):
                self._loaded_skill_names.append(w.removeprefix("/unskill ").strip())
            elif w.startswith("/"):
                self._slash_words.append(w)  # 如 /skill、/review
            elif w:
                self._normal_words.append(w)      # 如 exit、clear

        self._slash_words = sorted(set(self._slash_words))
        self._skill_names = sorted({name for name in self._skill_names if name})
        self._loaded_skill_names = sorted({name for name in self._loaded_skill_names if name})
        self._normal_words = sorted(set(self._normal_words))

    def get_completions(
        self, document: Document, complete_event,
    ) -> Iterable[Completion]:
        text = document.text_before_cursor

        if text.startswith("/"):
            if text.startswith("/skill "):
                prefix = text.removeprefix("/skill ")
                yield from self._name_completions(
                    self._skill_names,
                    prefix,
                    "skill",
                )
                return

            if text.startswith("/unskill "):
                prefix = text.removeprefix("/unskill ")
                yield from self._name_completions(
                    self._loaded_skill_names,
                    prefix,
                    "loaded skill",
                )
                return

            # ── slash 模式：首字符 / 时弹出内置 slash 命令和 /<skill> 快捷项 ──
            prefix = text
            for c in self._slash_words:
                if prefix and not c.lower().startswith(prefix.lower()):
                    continue
                yield Completion(
                    text=c,
                    start_position=-len(prefix),
                    display=c,
                    display_meta=self._slash_meta(c),
                )
        else:
            # ── 普通模式：只补全非 / 开头的单词 ──
            word = text.split()[-1] if text.split() else ""
            for c in self._normal_words:
                if word and not c.lower().startswith(word.lower()):
                    continue
                yield Completion(
                    text=c,
                    start_position=-len(word),
                )

    @staticmethod
    def _name_completions(
        candidates: list[str],
        prefix: str,
        meta: str,
    ) -> Iterable[Completion]:
        for c in candidates:
            if prefix and not c.lower().startswith(prefix.lower()):
                continue
            yield Completion(
                text=c,
                start_position=-len(prefix),
                display=c,
                display_meta=meta,
            )

    @staticmethod
    def _slash_meta(candidate: str) -> str:
        if candidate in ("/skill", "/skills"):
            return "list/load skills"
        if candidate == "/unskill":
            return "unload skill"
        if candidate == "/bye":
            return "exit"
        return "skill"


class TerminalUI:
    """基于 rich/prompt_toolkit 的终端交互界面。"""

    def __init__(
        self,
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        input_func: Callable[[str], str] = input,
    ) -> None:
        """允许测试注入输入输出流，真实运行时默认使用标准流。"""
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.input_func = input_func
        self.console = Console(file=self.stdout, highlight=False, theme=DONG_LIGHT_THEME)
        self.err_console = Console(file=self.stderr, highlight=False, theme=DONG_LIGHT_THEME)
        self._session: PromptSession[str] | None = None
        self._background_input_mode = False

    def _interactive(self) -> bool:
        """判断当前是否处于可交互 TTY，用于决定是否启用 prompt_toolkit。"""
        return bool(
            getattr(sys.stdin, "isatty", lambda: False)()
            and getattr(self.stdout, "isatty", lambda: False)()
        )

    def set_background_input_mode(self, enabled: bool) -> None:
        """切换后台任务输入模式；此模式下避免使用会抢占光标的动态 status。"""
        self._background_input_mode = enabled

    def read_prompt(
        self,
        completions: Iterable[str] = (),
        *,
        prompt_text: str = "\ndong ",
        bottom_toolbar: str | None = None,
    ) -> str:
        """读取一次 REPL 输入；非 TTY 环境下退回到普通 input()。"""
        if not self._interactive():
            return self.input_func(prompt_text)

        if self._session is None:
            self._session = PromptSession(history=InMemoryHistory())

        words = sorted({word for word in completions if word})
        completer = _SlashAwareCompleter(words)
        key_bindings = KeyBindings()

        @key_bindings.add("enter")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            # Enter 提交当前缓冲区，保持单行输入的直觉行为。
            event.app.current_buffer.validate_and_handle()

        @key_bindings.add("c-j")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            # Ctrl-J 插入换行，让 REPL 仍然支持多行 prompt。
            event.current_buffer.insert_text("\n")

        @key_bindings.add("/")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            # 首字符 / 时立即弹出 slash 命令/skill 菜单，贴近 Claude Code 的交互。
            event.current_buffer.insert_text("/")
            if event.current_buffer.document.text_before_cursor == "/":
                event.current_buffer.start_completion(select_first=False)

        @key_bindings.add(Keys.Any)
        def _(event) -> None:  # type: ignore[no-untyped-def]
            data = event.data
            if not data or not data.isprintable():
                return

            event.current_buffer.insert_text(data)
            if event.current_buffer.document.text_before_cursor.startswith("/"):
                event.current_buffer.start_completion(select_first=False)

        with patch_stdout():
            return self._session.prompt(
                prompt_text,
                completer=completer,
                complete_while_typing=True,
                multiline=True,
                enable_history_search=True,
                key_bindings=key_bindings,
                bottom_toolbar=bottom_toolbar,
            )

    def confirm_dangerous_command(self, command: str, default: str = "n") -> bool:
        """对危险命令进行显式确认，默认拒绝。"""
        default = default.lower()
        suffix = " [Y/n] " if default == "y" else " [y/N] "
        self.err_console.print("[bold red]Dangerous command[/]")
        self.err_console.print(Text(command, style="bold red"))
        answer = self.input_func(f"Run command?{suffix}").strip().lower()
        return answer in ("y", "yes") or (answer == "" and default == "y")

    def show_auto_skill(self, name: str, reason: str) -> None:
        """展示本轮自动选择的 skill，让隐式上下文变成可见决策。"""
        self.err_console.print(f"  Auto skill: {name} ({reason})")

    def show_startup(
        self,
        *,
        model: str,
        workdir: str,
        agents_loaded: bool,
        tools: Iterable[str],
    ) -> None:
        """渲染 CLI 启动信息，帮助用户确认模型、目录和工具范围。"""
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold cyan")
        table.add_column()
        table.add_row("model", model)
        table.add_row("workdir", workdir)
        table.add_row("DONG.md", "loaded" if agents_loaded else "not found")
        table.add_row("tools", ", ".join(tools))
        self.err_console.print("[bold cyan]dong[/]")
        self.err_console.print(table)

    def show_repl_help(self, *, skill_count: int) -> None:
        """显示 REPL 的固定命令提示和可用 skill 数量。"""
        if skill_count:
            self.err_console.print(
                f"   Skills: {skill_count} available  (/skill to list, /skill <name> to load)"
            )
        self.err_console.print(
            "   Commands: exit, clear, dir=<path>, /skill, /unskill, /<skill> <prompt>"
        )

    def show_loaded_skill(self, name: str, source: str, *, indent: str = "  ") -> None:
        """提示某个 skill 已加载，并展示来源。"""
        self.err_console.print(f"{indent}[green]Loaded skill:[/] {name} ({source})")

    def show_skill_already_loaded(self, name: str, source: str) -> None:
        """提示重复加载 skill 时的当前状态。"""
        self.err_console.print(f"  Skill already loaded: {name} ({source})")

    def show_removed_skill(self, name: str) -> None:
        """提示 skill 已从当前 REPL 上下文移除。"""
        self.err_console.print(f"  Removed skill: {name}")

    def show_skill_not_loaded(self, name: str) -> None:
        """提示用户尝试移除的 skill 当前并未加载。"""
        self.err_console.print(f"  Skill '{name}' not loaded")

    def show_skill_error(self, error: Exception) -> None:
        """展示 skill 加载或解析错误。"""
        self.err_console.print(f"  {error}")

    def show_unknown_command_or_skill(self, command: str) -> None:
        """展示未知 slash 命令或 skill 的错误提示。"""
        self.err_console.print(f"  Unknown command or skill: /{command}. Use /skill to list.")

    def show_context_cleared(self) -> None:
        """提示当前会话上下文已清空。"""
        self.err_console.print("  (context cleared)")

    def show_workdir(self, workdir: str) -> None:
        """提示工作目录已切换。"""
        self.err_console.print(f"  workdir -> {workdir}")

    def show_input_queued(self, *, pending: int) -> None:
        """提示用户输入已排队，会在当前任务后继续执行。"""
        suffix = f" ({pending} pending)" if pending > 1 else ""
        self.err_console.print(f"  [#57606a]queued input{suffix}[/]")

    def show_user_message(self, text: str) -> None:
        """在 stderr 回显用户本轮提示，让 REPL 历史更易追溯。"""
        self.err_console.print(f"  [bold]# user[/] {text}")

    def show_tool_cancelled(self, name: str, args_raw: str) -> None:
        """展示工具调用被取消的结果。"""
        display_args = self._display_args(args_raw)
        self.err_console.print(f"  [red]cancelled[/] {name}({display_args})")

    def show_tool_result(self, name: str, args_raw: str, result: ToolResult) -> None:
        """展示工具调用结果，成功和失败使用不同颜色。"""
        display_args = self._display_args(args_raw)
        status = "ok" if result.success else "failed"
        style = "green" if result.success else "red"
        summary = result.summary or result.error or "(no summary)"
        self.err_console.print(
            f"  [{style}]{status}[/] {name}({display_args}) - {summary}"
        )
        if name == "update_plan" and result.detail.strip():
            self.err_console.print("[bold #0b5cad]plan[/]")
            self.err_console.print(_markdown(result.detail))
        elif not result.success:
            detail = (result.error or result.detail).strip()
            if detail and detail != summary:
                self.err_console.print("[bold red]tool detail[/]")
                self.err_console.print(Text(self._display_detail(detail)))

    def show_working(
        self,
        message: str,
        *,
        timeout_seconds: float | None = None,
        cancel_hint: str = "Ctrl-C 取消当前任务",
    ) -> AbstractContextManager[None]:
        """显示当前正在执行的阶段；退出上下文时自动清理状态行。"""
        if self._background_input_mode:
            return _StaticWorkingStatus(
                self.err_console,
                message,
                timeout_seconds=timeout_seconds,
                cancel_hint=cancel_hint,
            )
        return _WorkingStatus(
            self.err_console,
            message,
            timeout_seconds=timeout_seconds,
            cancel_hint=cancel_hint,
        )

    def stream_assistant_message(self) -> AbstractContextManager[Callable[[str], None]]:
        """流式输出 assistant 文本增量；结束后由调用方决定是否补渲染最终消息。"""
        return _AssistantMessageStream(self.console)

    def show_assistant_message(self, text: str) -> None:
        """把模型最终文本按 Markdown 渲染到 stdout。"""
        normalized = _assistant_display_text(text)
        if normalized:
            self.console.print("[bold #0b5cad]assistant[/]")
            self.console.print(_markdown(normalized))

    def show_reasoning_message(self, text: str) -> None:
        """把模型返回的 reasoning_content 作为低调的 thinking 区块展示。"""
        normalized = _normalize_model_text(text)
        if not normalized:
            return

        self.err_console.print("[#57606a]thinking[/]")
        self.err_console.print(_markdown(normalized))

    def show_warning(self, message: str) -> None:
        """渲染黄色警告。"""
        self.err_console.print(f"[yellow]Warning:[/] {message}")

    def show_error(self, message: str) -> None:
        """渲染红色错误。"""
        self.err_console.print(f"[red]Error:[/] {message}")

    def blank_line(self) -> None:
        """在 stderr 输出一个空行，通常用于中断后的视觉分隔。"""
        self.err_console.print()

    @staticmethod
    def _display_args(args_raw: str, limit: int = 60) -> str:
        """截断过长工具参数，避免状态行过宽。"""
        return args_raw[:limit] + ("..." if len(args_raw) > limit else "")

    @staticmethod
    def _display_detail(detail: str, limit: int = 4000) -> str:
        """截断过长失败明细，避免错误面板刷屏。"""
        if len(detail) <= limit:
            return detail
        return detail[:limit].rstrip() + f"\n... (truncated, {len(detail)} total chars)"


def _normalize_model_text(text: str) -> str:
    """归一化模型文本，避免整体缩进被 Markdown 误判为代码块。"""
    return textwrap.dedent(text).strip()


def _markdown(text: str) -> Markdown:
    """使用 light 风格 Markdown，避免默认黑底 inline code/code block。"""
    return Markdown(
        text,
        code_theme=MARKDOWN_CODE_THEME,
        inline_code_theme=MARKDOWN_CODE_THEME,
    )


class _WorkingStatus(AbstractContextManager[None]):
    """后台刷新 Rich status，让同步阻塞调用也能展示耗时和超时阶段。"""

    def __init__(
        self,
        console: Console,
        message: str,
        *,
        timeout_seconds: float | None,
        cancel_hint: str,
        refresh_interval: float = 0.5,
    ) -> None:
        self.console = console
        self.message = message
        self.timeout_seconds = timeout_seconds
        self.cancel_hint = cancel_hint
        self.refresh_interval = refresh_interval
        self._stop = Event()
        self._started = 0.0
        self._status = None
        self._thread: Thread | None = None

    def __enter__(self) -> None:
        self._started = time.monotonic()
        self._status = self.console.status(
            _format_working_message(
                self.message,
                elapsed_seconds=0,
                timeout_seconds=self.timeout_seconds,
                cancel_hint=self.cancel_hint,
            ),
            spinner="dots",
        )
        self._status.__enter__()
        self._thread = Thread(target=self._refresh, daemon=True)
        self._thread.start()
        return None

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._status is not None:
            self._status.__exit__(exc_type, exc, traceback)
        return False

    def _refresh(self) -> None:
        while not self._stop.wait(self.refresh_interval):
            elapsed = int(time.monotonic() - self._started)
            if self._status is not None:
                self._status.update(
                    _format_working_message(
                        self.message,
                        elapsed_seconds=elapsed,
                        timeout_seconds=self.timeout_seconds,
                        cancel_hint=self.cancel_hint,
                    )
                )


class _StaticWorkingStatus(AbstractContextManager[None]):
    """后台输入模式下的非动态工作提示，避免与输入行争抢光标。"""

    def __init__(
        self,
        console: Console,
        message: str,
        *,
        timeout_seconds: float | None,
        cancel_hint: str,
    ) -> None:
        self.console = console
        self.message = message
        self.timeout_seconds = timeout_seconds
        self.cancel_hint = cancel_hint

    def __enter__(self) -> None:
        self.console.print(_format_working_message(
            self.message,
            elapsed_seconds=0,
            timeout_seconds=self.timeout_seconds,
            cancel_hint="继续输入会排队",
        ))
        return None

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


class _AssistantMessageStream(AbstractContextManager[Callable[[str], None]]):
    """把模型文本增量直接写到 stdout，避免工具调用前的说明被完整响应阻塞。"""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.started = False

    def __enter__(self) -> Callable[[str], None]:
        return self.write

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self.started:
            self.console.out("")
            self.console.file.flush()
        return False

    def write(self, delta: str) -> None:
        """写入一段模型文本；首个增量到达时再显示 assistant 标题。"""
        if not delta:
            return
        if not self.started:
            self.console.print("[bold cyan]assistant[/]")
            self.started = True
        self.console.out(delta, end="")
        self.console.file.flush()


def _format_working_message(
    message: str,
    *,
    elapsed_seconds: int,
    timeout_seconds: float | None,
    cancel_hint: str,
) -> str:
    """格式化运行中状态，包含耗时、超时阈值和取消提示。"""
    elapsed = max(0, elapsed_seconds)
    if timeout_seconds is None:
        suffix = f"{elapsed}s · {cancel_hint}"
    else:
        timeout_label = f"{timeout_seconds:g}s"
        if elapsed < timeout_seconds:
            suffix = f"{elapsed}s/{timeout_label} · {cancel_hint}"
        else:
            suffix = f"{elapsed}s/{timeout_label} · 已超过超时阈值，等待清理... · {cancel_hint}"
    return f"[cyan]{message}[/] [dim]{suffix}[/]"


def _assistant_display_text(text: str) -> str:
    """提取最终回答文本；兼容 JSON Output 包装后的 content/message/answer。"""
    normalized = _normalize_model_text(text)
    if not normalized or not normalized.startswith("{"):
        return normalized

    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError:
        return normalized

    if not isinstance(payload, dict):
        return normalized

    for key in ("content", "message", "answer"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _normalize_model_text(value)

    return normalized
