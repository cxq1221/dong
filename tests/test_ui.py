"""Tests for the terminal UI adapter."""

from __future__ import annotations

from io import StringIO

from dong.tool import ToolResult
from dong.ui import TerminalUI


def test_startup_rendering_includes_stable_fields() -> None:
    err = StringIO()
    ui = TerminalUI(stderr=err)

    ui.show_startup(
        model="gpt-test",
        workdir="/tmp/project",
        agents_loaded=True,
        tools=["read", "bash"],
    )

    rendered = err.getvalue()
    assert "dong" in rendered
    assert "gpt-test" in rendered
    assert "/tmp/project" in rendered
    assert "read, bash" in rendered


def test_tool_result_rendering_includes_status_name_and_summary() -> None:
    err = StringIO()
    ui = TerminalUI(stderr=err)

    ui.show_tool_result(
        "read",
        '{"filepath": "README.md"}',
        ToolResult(success=True, summary="README.md (3 lines)"),
    )

    rendered = err.getvalue()
    assert "ok" in rendered
    assert "read(" in rendered
    assert "README.md (3 lines)" in rendered


def test_assistant_message_renders_markdown_text() -> None:
    out = StringIO()
    ui = TerminalUI(stdout=out)

    ui.show_assistant_message("# Done\n\n- item")

    rendered = out.getvalue()
    assert "Done" in rendered
    assert "item" in rendered


def test_confirm_dangerous_command_respects_default_no() -> None:
    err = StringIO()
    ui = TerminalUI(stderr=err, input_func=lambda _prompt: "")

    assert ui.confirm_dangerous_command("rm -rf tmp", default="n") is False
    assert "rm -rf tmp" in err.getvalue()


def test_read_prompt_falls_back_outside_tty() -> None:
    ui = TerminalUI(stdout=StringIO(), input_func=lambda prompt: f"{prompt}hello")

    assert ui.read_prompt(["/skill"]) == "\n>>> hello"
