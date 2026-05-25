"""Terminal UI adapters for dong."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from typing import TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from dong.tool import ToolResult


class TerminalUI:
    """Rich/prompt_toolkit-backed terminal interface."""

    def __init__(
        self,
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        input_func: Callable[[str], str] = input,
    ) -> None:
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.input_func = input_func
        self.console = Console(file=self.stdout, highlight=False)
        self.err_console = Console(file=self.stderr, highlight=False)
        self._session: PromptSession[str] | None = None

    def _interactive(self) -> bool:
        return bool(
            getattr(sys.stdin, "isatty", lambda: False)()
            and getattr(self.stdout, "isatty", lambda: False)()
        )

    def read_prompt(self, completions: Iterable[str] = ()) -> str:
        """Read one REPL prompt, falling back to input() outside a TTY."""
        if not self._interactive():
            return self.input_func("\n>>> ")

        if self._session is None:
            self._session = PromptSession(history=InMemoryHistory())

        words = sorted({word for word in completions if word})
        completer = WordCompleter(words, ignore_case=True, sentence=True)
        key_bindings = KeyBindings()

        @key_bindings.add("enter")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            event.app.current_buffer.validate_and_handle()

        @key_bindings.add("c-j")
        def _(event) -> None:  # type: ignore[no-untyped-def]
            event.current_buffer.insert_text("\n")

        return self._session.prompt(
            "\n>>> ",
            completer=completer,
            complete_while_typing=True,
            multiline=True,
            enable_history_search=True,
            key_bindings=key_bindings,
        )

    def confirm_dangerous_command(self, command: str, default: str = "n") -> bool:
        """Confirm a dangerous command with the user."""
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
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold cyan")
        table.add_column()
        table.add_row("model", model)
        table.add_row("workdir", workdir)
        table.add_row("AGENTS.md", "loaded" if agents_loaded else "not found")
        table.add_row("tools", ", ".join(tools))
        self.err_console.print(Panel(table, title="dong", border_style="cyan"))

    def show_repl_help(self, *, skill_count: int) -> None:
        if skill_count:
            self.err_console.print(
                f"   Skills: {skill_count} available  (/skill to list, /skill <name> to load)"
            )
        self.err_console.print(
            "   Commands: exit, clear, dir=<path>, /skill, /unskill, /<skill> <prompt>"
        )

    def show_loaded_skill(self, name: str, source: str, *, indent: str = "  ") -> None:
        self.err_console.print(f"{indent}[green]Loaded skill:[/] {name} ({source})")

    def show_skill_already_loaded(self, name: str, source: str) -> None:
        self.err_console.print(f"  Skill already loaded: {name} ({source})")

    def show_removed_skill(self, name: str) -> None:
        self.err_console.print(f"  Removed skill: {name}")

    def show_skill_not_loaded(self, name: str) -> None:
        self.err_console.print(f"  Skill '{name}' not loaded")

    def show_skill_error(self, error: Exception) -> None:
        self.err_console.print(f"  {error}")

    def show_unknown_command_or_skill(self, command: str) -> None:
        self.err_console.print(f"  Unknown command or skill: /{command}. Use /skill to list.")

    def show_context_cleared(self) -> None:
        self.err_console.print("  (context cleared)")

    def show_workdir(self, workdir: str) -> None:
        self.err_console.print(f"  workdir -> {workdir}")

    def show_tool_cancelled(self, name: str, args_raw: str) -> None:
        display_args = self._display_args(args_raw)
        self.err_console.print(f"  [red]cancelled[/] {name}({display_args})")

    def show_tool_result(self, name: str, args_raw: str, result: ToolResult) -> None:
        display_args = self._display_args(args_raw)
        status = "ok" if result.success else "failed"
        style = "green" if result.success else "red"
        summary = result.summary or result.error or "(no summary)"
        self.err_console.print(
            f"  [{style}]{status}[/] {name}({display_args}) - {summary}"
        )

    def show_assistant_message(self, text: str) -> None:
        self.console.print(Markdown(text))

    def show_warning(self, message: str) -> None:
        self.err_console.print(Panel(message, title="Warning", border_style="yellow"))

    def show_error(self, message: str) -> None:
        self.err_console.print(Panel(message, title="Error", border_style="red"))

    def blank_line(self) -> None:
        self.err_console.print()

    @staticmethod
    def _display_args(args_raw: str, limit: int = 60) -> str:
        return args_raw[:limit] + ("..." if len(args_raw) > limit else "")
