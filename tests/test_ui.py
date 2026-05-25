"""终端 UI 适配层测试：覆盖启动信息、工具结果、Markdown 和确认输入。"""

from __future__ import annotations

from io import StringIO

from prompt_toolkit.document import Document

from dong.tool import ToolResult
from dong.ui import TerminalUI, _SlashAwareCompleter


def test_startup_rendering_includes_stable_fields() -> None:
    """启动渲染应包含模型、工作目录、AGENTS 状态和工具列表。"""
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
    """工具结果渲染应包含状态、工具名和摘要。"""
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
    """assistant 文本应按 Markdown 渲染到 stdout。"""
    out = StringIO()
    ui = TerminalUI(stdout=out)

    ui.show_assistant_message("# Done\n\n- item")

    rendered = out.getvalue()
    assert "Done" in rendered
    assert "item" in rendered


def test_confirm_dangerous_command_respects_default_no() -> None:
    """危险命令确认默认拒绝时，空输入应返回 False。"""
    err = StringIO()
    ui = TerminalUI(stderr=err, input_func=lambda _prompt: "")

    assert ui.confirm_dangerous_command("rm -rf tmp", default="n") is False
    assert "rm -rf tmp" in err.getvalue()


def test_read_prompt_falls_back_outside_tty() -> None:
    """非 TTY 环境下 read_prompt 应退回到 input_func。"""
    ui = TerminalUI(stdout=StringIO(), input_func=lambda prompt: f"{prompt}hello")

    assert ui.read_prompt(["/skill"]) == "\ndong hello"


def _completion_texts(completer: _SlashAwareCompleter, text: str) -> list[str]:
    return [item.text for item in completer.get_completions(Document(text), None)]


def test_slash_completer_shows_skill_shortcuts_when_first_character_is_slash() -> None:
    """首字符输入 / 时，应弹出内置 slash 命令和可用 skill 快捷命令。"""
    completer = _SlashAwareCompleter([
        "clear",
        "/skill",
        "/unskill",
        "/review",
        "/python-test",
        "/skill review",
    ])

    completions = _completion_texts(completer, "/")

    assert "/skill" in completions
    assert "/review" in completions
    assert "/python-test" in completions
    assert "clear" not in completions


def test_slash_completer_completes_skill_names_after_skill_command() -> None:
    """/skill 后应补 skill 名称本身，而不是插入完整 /skill <name>。"""
    completer = _SlashAwareCompleter([
        "/skill",
        "/unskill",
        "/skill review",
        "/skill python-test",
    ])

    completions = _completion_texts(completer, "/skill ")

    assert "review" in completions
    assert "python-test" in completions
    assert "/skill review" not in completions
