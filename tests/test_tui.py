"""Fullscreen TUI behavior tests."""

from __future__ import annotations

import threading

from prompt_toolkit.document import Document

from dong.tool import ToolResult
from dong.tui import TuiApp, _fit_to_width, render_markdown


def _content_line_text(content, line_no: int) -> str:  # type: ignore[no-untyped-def]
    return "".join(fragment[1] for fragment in content.get_line(line_no))


def _completion_texts(app: TuiApp, text: str) -> list[str]:
    completer = app.composer.completer
    assert completer is not None
    return [item.text for item in completer.get_completions(Document(text), None)]


def test_rich_markdown_renders_offscreen_with_ansi_color() -> None:
    """Rich Markdown 应离屏渲染成带 ANSI 的 transcript 文本。"""
    rendered = render_markdown("# Title\n\n`code`")

    assert "Title" in rendered
    assert "code" in rendered
    assert "\x1b[" in rendered


def test_rich_markdown_respects_narrow_width() -> None:
    """窄宽度下 Rich 离屏渲染应按指定宽度换行，而不是固定 100 列。"""
    rendered = render_markdown(
        "这是一个很长的句子，用来验证窄终端宽度下不会仍然按照一百列排版。",
        width=24,
    )

    plain = rendered.replace("\x1b[0m", "")
    assert len(plain.splitlines()) > 1


def test_status_text_is_truncated_to_display_width() -> None:
    """状态栏是单行区域，宽度不足时应主动截断并显示省略号。"""
    fitted = _fit_to_width("正在执行工具：bash 1234567890", 12)

    assert fitted.endswith("…")


def test_tui_ui_updates_streaming_assistant_and_thinking_items() -> None:
    """assistant/thinking streaming 应更新同一个 transcript item，而不是逐 token 追加。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])

    with app.ui.stream_assistant_message() as write_assistant:
        write_assistant("Hel")
        write_assistant("lo")
    with app.ui.stream_reasoning_message() as write_reasoning:
        write_reasoning("Think")
        write_reasoning("ing")

    assert "Hello" in app.transcript_text
    assert "Thinking" in app.transcript_text
    assert len(app._transcript) == 2


def test_tui_worker_processes_queued_inputs_in_order() -> None:
    """工作中提交的输入应按队列顺序交给 process_input。"""
    seen: list[str] = []
    first_started = threading.Event()
    release_first = threading.Event()

    def process_input(text: str, _ui) -> bool:  # type: ignore[no-untyped-def]
        seen.append(text)
        if text == "first":
            first_started.set()
            assert release_first.wait(timeout=1)
        return False

    app = TuiApp(process_input=process_input, completion_provider=lambda: [])
    app._worker_thread.start()
    try:
        app.submit_text("first")
        assert first_started.wait(timeout=1)
        app.submit_text("second")
        assert app.status.queued == 1
        release_first.set()
        app._input_queue.join()
    finally:
        app._input_queue.put(None)
        app._worker_thread.join(timeout=1)

    assert seen == ["first", "second"]


def test_tui_confirmation_blocks_until_answered() -> None:
    """危险命令确认应由 TUI 同步等待用户回答。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    result: list[bool] = []
    thread = threading.Thread(
        target=lambda: result.append(app.request_confirmation("rm -rf tmp", "n")),
    )

    thread.start()
    assert app.composer.buffer.read_only()
    app.answer_confirmation(True)
    thread.join(timeout=1)

    assert result == [True]
    assert not app.composer.buffer.read_only()


def test_tui_slash_completer_filters_skills() -> None:
    """composer 应继续支持 slash skill 菜单和过滤。"""
    app = TuiApp(
        process_input=lambda _text, _ui: False,
        completion_provider=lambda: ["/skill", "/review", "/python-test", "clear"],
    )

    assert "/review" in _completion_texts(app, "/")
    assert "clear" not in _completion_texts(app, "/")
    assert _completion_texts(app, "/re") == ["/review"]


def test_tui_tool_result_preserves_update_plan_detail() -> None:
    """update_plan 的完整 detail 应进入 transcript。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])

    app.ui.show_tool_result(
        "update_plan",
        "{}",
        ToolResult(success=True, summary="updated", detail="1. first\n2. second"),
    )

    assert "first" in app.transcript_text
    assert "second" in app.transcript_text


def test_tui_tool_result_uses_current_render_width() -> None:
    """TuiUI 渲染 transcript 时应使用当前 TUI 宽度。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app._render_width_override = 24

    app.ui.show_assistant_message("这是一个很长的句子，用来验证 TUI 宽度传入 Rich renderer。")

    assert len(app._transcript[0].ansi.splitlines()) > 1


def test_transcript_cursor_tracks_bottom_page() -> None:
    """transcript 超过一屏时，prompt_toolkit 光标应锚定当前视图底部。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "\n".join(f"line {index}" for index in range(60)))

    content = app.transcript_control.create_content(width=80, height=20)

    assert content.cursor_position.y == 59
    assert _content_line_text(content, 0) == "line 0"
    assert _content_line_text(content, 59) == "line 59"


def test_transcript_manual_scroll_changes_visible_slice() -> None:
    """PageUp/滚轮滚动应改变 transcript 视图，而不是被默认窗口滚动重置。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "\n".join(f"line {index}" for index in range(60)))
    app.transcript_control.create_content(width=80, height=20)

    app.scroll_transcript(20)
    content = app.transcript_control.create_content(width=80, height=20)

    assert content.cursor_position.y == 39
    assert _content_line_text(content, content.line_count - 1) == "line 39"


def test_tui_working_status_records_tool_start() -> None:
    """工具开始执行时应进入 transcript，并同步更新状态栏。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])

    with app.ui.show_working("正在执行工具：bash", timeout_seconds=30):
        assert "正在执行工具：bash" in app.status.label

    assert "running 正在执行工具：bash" in app.transcript_text
