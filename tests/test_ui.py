"""终端 UI 适配层测试：覆盖启动信息、工具结果、Markdown 和确认输入。"""

from __future__ import annotations

from io import StringIO

from prompt_toolkit.document import Document

from dong.tool import ToolResult
from dong.ui import TerminalUI, _SlashAwareCompleter, _format_working_message, _markdown


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
    assert "╭" not in rendered
    assert "│" not in rendered


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


def test_update_plan_tool_result_renders_full_detail() -> None:
    """update_plan 的完整计划在 detail 中，应展示给用户而不只展示摘要。"""
    err = StringIO()
    ui = TerminalUI(stderr=err)

    ui.show_tool_result(
        "update_plan",
        '{"plan": []}',
        ToolResult(
            success=True,
            summary="Updated plan (3 steps)",
            detail=(
                "Need a short implementation path.\n\n"
                "1. [completed] Inspect tool registry\n"
                "2. [in_progress] Add plan tool\n"
                "3. [pending] Run focused tests"
            ),
        ),
    )

    rendered = err.getvalue()
    assert "Updated plan (3 steps)" in rendered
    assert "Need a short implementation path." in rendered
    assert "Inspect tool registry" in rendered
    assert "Add plan tool" in rendered
    assert "Run focused tests" in rendered


def test_failed_tool_result_renders_error_detail() -> None:
    """失败工具应展示 detail/error，避免只看到 exit code 摘要。"""
    err = StringIO()
    ui = TerminalUI(stderr=err)

    ui.show_tool_result(
        "bash",
        '{"command": "bad"}',
        ToolResult(
            success=False,
            summary="exit code 127",
            detail="bad: command not found",
        ),
    )

    rendered = err.getvalue()
    assert "failed" in rendered
    assert "exit code 127" in rendered
    assert "bad: command not found" in rendered


def test_working_message_shows_elapsed_timeout_and_cancel_hint() -> None:
    """运行中状态应展示耗时、超时阈值和取消提示。"""
    running = _format_working_message(
        "正在执行工具：bash",
        elapsed_seconds=12,
        timeout_seconds=30,
        cancel_hint="Ctrl-C 取消当前任务",
    )
    timed_out = _format_working_message(
        "正在执行工具：bash",
        elapsed_seconds=31,
        timeout_seconds=30,
        cancel_hint="Ctrl-C 取消当前任务",
    )

    assert "12s/30s" in running
    assert "Ctrl-C 取消当前任务" in running
    assert "已超过超时阈值" in timed_out


def test_assistant_message_renders_markdown_text() -> None:
    """assistant 文本应按 Markdown 渲染到 stdout。"""
    out = StringIO()
    ui = TerminalUI(stdout=out)

    ui.show_assistant_message("# Done\n\n- item")

    rendered = out.getvalue()
    assert "assistant" in rendered
    assert "Done" in rendered
    assert "item" in rendered
    assert "╭" not in rendered
    assert "│" not in rendered


def test_markdown_uses_light_code_style_without_background() -> None:
    """Markdown code 样式应接近 light VS Code，避免默认黑底高反差。"""
    ui = TerminalUI(stdout=StringIO())
    code_style = ui.console.get_style("markdown.code")
    block_style = ui.console.get_style("markdown.code_block")
    markdown = _markdown("`cli.py`\n\n```python\nprint('x')\n```")

    assert code_style.bgcolor is None
    assert block_style.bgcolor is None
    assert markdown.code_theme == "ansi_light"
    assert markdown.inline_code_theme == "ansi_light"


def test_assistant_message_dedents_accidental_leading_spaces() -> None:
    """模型整段文本带缩进时，不应被 Markdown 误渲染成代码块。"""
    out = StringIO()
    ui = TerminalUI(stdout=out)

    ui.show_assistant_message("    **{name}** answer")

    rendered = out.getvalue()
    assert "{name} answer" in rendered
    assert "**{name}**" not in rendered


def test_assistant_message_unwraps_single_key_json_output_content() -> None:
    """单字段 JSON Output 包装的最终回答应只渲染 content。"""
    out = StringIO()
    ui = TerminalUI(stdout=out)

    ui.show_assistant_message('{"content": "# 产品文档\\n\\n- 中文内容"}')

    rendered = out.getvalue()
    assert "assistant" in rendered
    assert "产品文档" in rendered
    assert "中文内容" in rendered
    assert '"content"' not in rendered
    assert '"format"' not in rendered


def test_assistant_message_preserves_multi_key_json_output() -> None:
    """多字段 JSON 不应被误判为纯回答包装，避免丢失模型输出结构。"""
    out = StringIO()
    ui = TerminalUI(stdout=out)

    ui.show_assistant_message(
        '{"content": "# 产品文档\\n\\n- 中文内容", "format": "markdown"}'
    )

    rendered = out.getvalue()
    assert '"content"' in rendered
    assert '"format"' in rendered


def test_reasoning_message_renders_thinking_panel() -> None:
    """reasoning_content 应作为 thinking 区块输出到 stderr。"""
    err = StringIO()
    ui = TerminalUI(stderr=err)

    ui.show_reasoning_message("I should inspect the file first.")

    rendered = err.getvalue()
    assert "thinking" in rendered
    assert "I should inspect the file first." in rendered


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


def test_slash_completer_filters_skill_shortcuts_as_user_types() -> None:
    """slash 菜单打开后继续输入文本，应按当前前缀过滤可见 skill 选项。"""
    completer = _SlashAwareCompleter([
        "/skill",
        "/unskill",
        "/review",
        "/python-test",
        "/run-tests",
        "/skill review",
    ])

    completions = _completion_texts(completer, "/re")

    assert completions == ["/review"]


def test_slash_completer_filters_skill_names_after_skill_command() -> None:
    """/skill 菜单打开后继续输入名称，也应只保留匹配 skill。"""
    completer = _SlashAwareCompleter([
        "/skill",
        "/unskill",
        "/skill review",
        "/skill python-test",
        "/skill run-tests",
    ])

    completions = _completion_texts(completer, "/skill re")

    assert completions == ["review"]
