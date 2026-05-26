"""Fullscreen TUI behavior tests."""

from __future__ import annotations

import threading

from prompt_toolkit.document import Document

from dong.tool import ToolResult
from dong.tui import TuiApp, render_markdown


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


def test_tui_working_status_records_tool_start() -> None:
    """工具开始执行时应进入 transcript，并同步更新状态栏。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])

    with app.ui.show_working("正在执行工具：bash", timeout_seconds=30):
        assert "正在执行工具：bash" in app.status.label

    assert "running 正在执行工具：bash" in app.transcript_text
