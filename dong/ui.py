"""dong 的终端 UI 适配层，集中封装 rich 和 prompt_toolkit 输出。"""

from __future__ import annotations

import sys
import textwrap
from collections.abc import Callable, Iterable
from typing import TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from dong.tool import ToolResult


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
        self.console = Console(file=self.stdout, highlight=False)
        self.err_console = Console(file=self.stderr, highlight=False)
        self._session: PromptSession[str] | None = None

    def _interactive(self) -> bool:
        """判断当前是否处于可交互 TTY，用于决定是否启用 prompt_toolkit。"""
        return bool(
            getattr(sys.stdin, "isatty", lambda: False)()
            and getattr(self.stdout, "isatty", lambda: False)()
        )

    def read_prompt(self, completions: Iterable[str] = ()) -> str:
        """读取一次 REPL 输入；非 TTY 环境下退回到普通 input()。"""
        if not self._interactive():
            return self.input_func("\ndong ")

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

        return self._session.prompt(
            "\ndong ",
            completer=completer,
            complete_while_typing=True,
            multiline=True,
            enable_history_search=True,
            key_bindings=key_bindings,
        )

    def confirm_dangerous_command(self, command: str, default: str = "n") -> bool:
        """对危险命令进行显式确认，默认拒绝。"""
        default = default.lower()
        suffix = " [Y/n] " if default == "y" else " [y/N] "
        self.err_console.print(
            Panel(
                Text(command, style="bold red"),
                title="Dangerous command",
                border_style="red",
            )
        )
        answer = self.input_func(f"Run command?{suffix}").strip().lower()
        return answer in ("y", "yes") or (answer == "" and default == "y")

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
        table.add_row("AGENTS.md", "loaded" if agents_loaded else "not found")
        table.add_row("tools", ", ".join(tools))
        self.err_console.print(Panel(table, title="dong", border_style="cyan"))

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

    def show_assistant_message(self, text: str) -> None:
        """把模型最终文本按 Markdown 渲染到 stdout。"""
        normalized = _normalize_model_text(text)
        if normalized:
            self.console.print(Markdown(normalized))

    def show_reasoning_message(self, text: str) -> None:
        """把模型返回的 reasoning_content 作为低调的 thinking 区块展示。"""
        normalized = _normalize_model_text(text)
        if not normalized:
            return

        self.err_console.print(
            Panel(
                Markdown(normalized),
                title="thinking",
                border_style="bright_black",
            )
        )

    def show_warning(self, message: str) -> None:
        """渲染黄色警告面板。"""
        self.err_console.print(Panel(message, title="Warning", border_style="yellow"))

    def show_error(self, message: str) -> None:
        """渲染红色错误面板。"""
        self.err_console.print(Panel(message, title="Error", border_style="red"))

    def blank_line(self) -> None:
        """在 stderr 输出一个空行，通常用于中断后的视觉分隔。"""
        self.err_console.print()

    @staticmethod
    def _display_args(args_raw: str, limit: int = 60) -> str:
        """截断过长工具参数，避免状态行过宽。"""
        return args_raw[:limit] + ("..." if len(args_raw) > limit else "")


def _normalize_model_text(text: str) -> str:
    """归一化模型文本，避免整体缩进被 Markdown 误判为代码块。"""
    return textwrap.dedent(text).strip()
