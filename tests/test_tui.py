"""Fullscreen TUI behavior tests."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.keys import Keys
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from dong.ocr import image_marker
from dong.tool import ToolResult
from dong.tui import TuiApp, _fit_to_width, render_markdown, render_text


def _content_line_text(content, line_no: int) -> str:  # type: ignore[no-untyped-def]
    return "".join(fragment[1] for fragment in content.get_line(line_no))


def _completion_texts(app: TuiApp, text: str) -> list[str]:
    completer = app.composer.completer
    assert completer is not None
    return [item.text for item in completer.get_completions(Document(text), None)]


def _mouse_event(
    row: int,
    event_type: MouseEventType,
    *,
    button: MouseButton = MouseButton.LEFT,
    x: int = 0,
) -> MouseEvent:
    return MouseEvent(
        position=Point(x=x, y=row),
        event_type=event_type,
        button=button,
        modifiers=frozenset(),
    )


def _press_key(app: TuiApp, key: Keys | str) -> None:
    """执行指定 TUI key binding，便于测试 Ctrl-C 这类全局按键。"""
    for binding in app.key_bindings.bindings:
        if binding.keys == (key,):
            binding.handler(SimpleNamespace(app=app.application))
            return
    raise AssertionError(f"missing key binding: {key}")


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


def test_render_text_preserves_square_brackets() -> None:
    """普通文本渲染不能把方括号内容误当 Rich markup 吞掉。"""
    rendered = render_text("user", "literal [notatag] and [red] text")

    assert "[notatag]" in rendered
    assert "[red]" in rendered


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


def test_tui_paste_clipboard_image_inserts_visible_marker(tmp_path, monkeypatch) -> None:
    """TUI 粘贴图片应先在输入框插入可见附件占位符。"""
    image_path = tmp_path / "clipboard.png"
    image_path.write_bytes(b"fake png")
    app = TuiApp(
        process_input=lambda _text, _ui: False,
        completion_provider=lambda: [],
        workdir=str(tmp_path),
    )

    def fake_save(workdir: str):
        assert workdir == str(tmp_path)
        return image_path

    monkeypatch.setattr("dong.tui.save_clipboard_image", fake_save)

    app.paste_clipboard_image()

    assert image_marker(image_path) in app.composer.text
    assert "attached image" in app.transcript_text


def test_tui_up_down_navigates_submitted_user_messages() -> None:
    """上下方向键历史应只在用户已提交消息和当前草稿之间切换。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.submit_text("first")
    app.submit_text("second")
    app.composer.buffer.text = "draft"

    app.navigate_input_history(-1)
    assert app.composer.text == "second"

    app.navigate_input_history(-1)
    assert app.composer.text == "first"

    app.navigate_input_history(-1)
    assert app.composer.text == "first"

    app.navigate_input_history(1)
    assert app.composer.text == "second"

    app.navigate_input_history(1)
    assert app.composer.text == "draft"


def test_tui_exit_unblocks_pending_confirmation() -> None:
    """TUI 退出时应拒绝并唤醒危险命令确认，避免 worker 线程挂住。"""
    confirmation_seen = threading.Event()

    def process_input(_text: str, ui) -> bool:  # type: ignore[no-untyped-def]
        confirmation_seen.set()
        ui.confirm_dangerous_command("rm -rf tmp", "n")
        raise AssertionError("shutdown should abort the active confirmation")

    app = TuiApp(process_input=process_input, completion_provider=lambda: [])
    app._worker_thread.start()

    app.submit_text("run")
    assert confirmation_seen.wait(timeout=1)
    assert app.composer.buffer.read_only()

    app.request_exit()
    app._worker_thread.join(timeout=1)

    assert not app._worker_thread.is_alive()
    assert not app.composer.buffer.read_only()


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


def test_tui_ui_shows_auto_skill_selection() -> None:
    """TUI 适配器应支持自动 skill 提示，避免自动路由路径崩溃。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])

    app.ui.show_auto_skill("chrome-cdp", "matched: 浏览器")

    assert "Matched skill: chrome-cdp" in app.transcript_text
    assert "mode      auto" in app.transcript_text
    assert "matched: 浏览器" in app.transcript_text


def test_tui_session_picker_selects_with_keyboard() -> None:
    """session picker 应支持上下键移动并用 Enter 选择。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    items = [
        SimpleNamespace(
            session_id="session-old",
            updated_at_ms=1000,
            message_count=1,
            prompt_preview="old prompt",
            assistant_preview="old answer",
        ),
        SimpleNamespace(
            session_id="session-new",
            updated_at_ms=2000,
            message_count=2,
            prompt_preview="new prompt",
            assistant_preview="new answer",
        ),
    ]
    result: list[str | None] = []
    thread = threading.Thread(
        target=lambda: result.append(app.select_session(items)),
        daemon=True,
    )

    thread.start()
    assert app._session_picker is not None
    assert "old prompt" in app.transcript_text

    assert app.move_session_picker_selection(1) is True
    assert app.accept_session_picker() is True
    thread.join(timeout=1)

    assert result == ["session-new"]
    assert app._session_picker is None
    app.ui.show_session_restored(
        "session-new",
        "dong -d . --resume session-new",
        [
            {"role": "user", "content": "restored user question"},
            {"role": "assistant", "content": "restored assistant answer"},
        ],
    )
    assert "Restored session: session-new" in app.transcript_text
    assert "Context loaded. Continue typing." in app.transcript_text
    assert "restored user question" in app.transcript_text
    assert "restored assistant answer" in app.transcript_text
    assert "Selected session:" not in app.transcript_text
    assert "old prompt" not in app.transcript_text


def test_tui_session_picker_selects_with_mouse() -> None:
    """session picker 应支持鼠标点击选择对应行。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.set_transcript_viewport_height(20)
    items = [
        SimpleNamespace(
            session_id="session-old",
            updated_at_ms=1000,
            message_count=1,
            prompt_preview="old prompt",
            assistant_preview="old answer",
        ),
        SimpleNamespace(
            session_id="session-new",
            updated_at_ms=2000,
            message_count=2,
            prompt_preview="new prompt",
            assistant_preview="new answer",
        ),
    ]
    result: list[str | None] = []
    thread = threading.Thread(
        target=lambda: result.append(app.select_session(items)),
        daemon=True,
    )

    thread.start()
    assert app._session_picker is not None
    assert app.handle_session_picker_mouse(18) is True
    thread.join(timeout=1)

    assert result == ["session-new"]


def test_tui_slash_completion_menu_is_vertical_and_cached() -> None:
    """slash 候选项应以竖向列表展示，并复用短缓存避免反复扫描。"""
    calls = 0

    def completions() -> list[str]:
        nonlocal calls
        calls += 1
        return ["/skill", "/review"]

    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=completions)
    app.composer.buffer.text = "/"
    rendered = "".join(text for _style, text in app._formatted_completion_menu())

    assert app._has_completion_menu()
    assert "› /skill\n    /review" in rendered
    assert "/review" not in str(app._formatted_status())
    assert "/review" in "".join(text for _style, text in app._formatted_completion_menu())
    assert calls == 1


def test_tui_slash_completion_menu_moves_selection_and_accepts_candidate() -> None:
    """slash 候选列表出现时，上下键语义应优先移动高亮项而不是切输入历史。"""
    app = TuiApp(
        process_input=lambda _text, _ui: False,
        completion_provider=lambda: ["/skill", "/review", "/python-test"],
    )
    app.submit_text("previous user message")
    app.composer.buffer.text = "/"

    assert app.move_completion_selection(1)
    rendered = "".join(text for _style, text in app._formatted_completion_menu())
    assert "  /skill\n  › /python-test" in rendered
    assert app.composer.text == "/"

    assert app.accept_completion_selection()
    assert app.composer.text == "/python-test"


def test_tui_defaults_to_mouse_mode_for_scrolling_and_selection() -> None:
    """TUI 默认捕获鼠标，统一处理滚轮、滚动条和内容区拖选复制。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])

    assert app.application.mouse_support() is True
    assert "mouse" in str(app._formatted_status())

    assert app.toggle_mouse_capture() is False

    assert app.application.mouse_support() is False
    assert "copy" in str(app._formatted_status())


def test_tui_transcript_ctrl_c_copies_visible_selection(monkeypatch) -> None:
    """内容区拖选只保留选区，Ctrl-C 才复制当前可视 transcript 文本。"""
    copied: list[str] = []
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "alpha\nbeta")
    app.transcript_control.create_content(width=80, height=20)

    def fake_copy(text: str, *, output=None) -> bool:  # type: ignore[no-untyped-def]
        copied.append(text)
        return True

    monkeypatch.setattr("dong.tui._copy_text_to_clipboard", fake_copy)

    app.transcript_control.mouse_handler(
        _mouse_event(0, MouseEventType.MOUSE_DOWN, x=1)
    )
    app.transcript_control.mouse_handler(
        _mouse_event(1, MouseEventType.MOUSE_MOVE, x=2)
    )
    app.transcript_control.mouse_handler(
        _mouse_event(1, MouseEventType.MOUSE_UP, x=2)
    )

    assert copied == []
    assert app._transcript_selection is not None
    assert "selected" in app.status.label

    _press_key(app, Keys.ControlC)

    assert copied == ["lpha\nbe"]
    assert app._last_copied_transcript_text == "lpha\nbe"
    assert app._transcript_selection is not None
    assert "copied" in app.status.label

    app.transcript_control.mouse_handler(
        _mouse_event(0, MouseEventType.MOUSE_DOWN, x=0)
    )
    app.transcript_control.mouse_handler(
        _mouse_event(0, MouseEventType.MOUSE_UP, x=0)
    )

    assert app._transcript_selection is None


def test_tui_transcript_selection_survives_scroll_and_clears_on_submit(monkeypatch) -> None:
    """滚动时保留选区；提交新消息会改变 transcript 语境，应清除旧选区。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "\n".join(f"line {index}" for index in range(60)))
    app.transcript_control.create_content(width=80, height=20)

    app.transcript_control.mouse_handler(
        _mouse_event(19, MouseEventType.MOUSE_DOWN, x=0)
    )
    app.transcript_control.mouse_handler(
        _mouse_event(19, MouseEventType.MOUSE_UP, x=4)
    )
    assert app._transcript_selection is not None

    app.scroll_transcript(3)
    assert app._transcript_selection is not None

    app.scroll_transcript(-3)
    assert app._transcript_selection is not None

    app.submit_text("next")
    assert app._transcript_selection is None


def test_tui_transcript_selection_uses_prompt_toolkit_text_columns(monkeypatch) -> None:
    """prompt_toolkit 已把鼠标坐标换算成字符列，中文选区不能再按双宽折算。"""
    copied: list[str] = []
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "中文ABC")
    app.transcript_control.create_content(width=80, height=20)

    def fake_copy(text: str, *, output=None) -> bool:  # type: ignore[no-untyped-def]
        copied.append(text)
        return True

    monkeypatch.setattr("dong.tui._copy_text_to_clipboard", fake_copy)

    app.transcript_control.mouse_handler(
        _mouse_event(0, MouseEventType.MOUSE_DOWN, x=1)
    )
    app.transcript_control.mouse_handler(
        _mouse_event(0, MouseEventType.MOUSE_UP, x=3)
    )

    assert copied == []
    assert app.copy_transcript_selection()
    assert copied == ["文A"]


def test_tui_transcript_selection_includes_last_character_at_line_end(monkeypatch) -> None:
    """拖到行尾最后一个字符附近时，应包含最后一个字。"""
    copied: list[str] = []
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "alpha")
    app.transcript_control.create_content(width=80, height=20)

    def fake_copy(text: str, *, output=None) -> bool:  # type: ignore[no-untyped-def]
        copied.append(text)
        return True

    monkeypatch.setattr("dong.tui._copy_text_to_clipboard", fake_copy)

    app.transcript_control.mouse_handler(
        _mouse_event(0, MouseEventType.MOUSE_DOWN, x=0)
    )
    app.transcript_control.mouse_handler(
        _mouse_event(0, MouseEventType.MOUSE_MOVE, x=4)
    )
    app.transcript_control.mouse_handler(
        _mouse_event(0, MouseEventType.MOUSE_UP, x=4)
    )

    assert app.copy_transcript_selection()
    assert copied == ["alpha"]


def test_tui_transcript_selection_ignores_blank_area_fallback(monkeypatch) -> None:
    """拖到无文本区域产生的 (0,0) 兜底坐标，不应反向选中上方内容。"""
    copied: list[str] = []
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "alpha\nbeta\ngamma")
    app.transcript_control.create_content(width=80, height=20)

    def fake_copy(text: str, *, output=None) -> bool:  # type: ignore[no-untyped-def]
        copied.append(text)
        return True

    monkeypatch.setattr("dong.tui._copy_text_to_clipboard", fake_copy)

    app.transcript_control.mouse_handler(
        _mouse_event(2, MouseEventType.MOUSE_DOWN, x=1)
    )
    app.transcript_control.mouse_handler(
        _mouse_event(2, MouseEventType.MOUSE_MOVE, x=4)
    )
    app.transcript_control.mouse_handler(
        _mouse_event(0, MouseEventType.MOUSE_MOVE, x=0)
    )
    app.transcript_control.mouse_handler(
        _mouse_event(0, MouseEventType.MOUSE_UP, x=0)
    )

    assert copied == []
    assert app.copy_transcript_selection()
    assert copied == ["amma"]


def test_tui_transcript_selection_does_not_start_from_blank_gap(monkeypatch) -> None:
    """两行之间空白处按下会得到 (0,0) 兜底，拖走时不应从顶部开始选区。"""
    copied: list[str] = []
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "alpha\nbeta")
    app.transcript_control.create_content(width=80, height=20)

    def fake_copy(text: str, *, output=None) -> bool:  # type: ignore[no-untyped-def]
        copied.append(text)
        return True

    monkeypatch.setattr("dong.tui._copy_text_to_clipboard", fake_copy)

    app.transcript_control.mouse_handler(
        _mouse_event(0, MouseEventType.MOUSE_DOWN, x=0)
    )
    app.transcript_control.mouse_handler(
        _mouse_event(1, MouseEventType.MOUSE_MOVE, x=4)
    )
    app.transcript_control.mouse_handler(
        _mouse_event(1, MouseEventType.MOUSE_UP, x=4)
    )

    assert app._transcript_selection is None
    assert not app.copy_transcript_selection()
    assert copied == []


def test_tui_composer_mouse_wheel_scrolls_transcript() -> None:
    """输入框区域滚轮也应滚动 transcript，而不是滚动多行输入框。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "\n".join(f"line {index}" for index in range(60)))
    app.transcript_control.create_content(width=80, height=20)

    app.composer.control.mouse_handler(_mouse_event(0, MouseEventType.SCROLL_UP))
    content = app.transcript_control.create_content(width=80, height=20)

    assert _content_line_text(content, content.line_count - 1) == "line 56"
    assert app._scroll_offset == 3


def test_tui_status_shows_context_usage() -> None:
    """TUI status bar 应展示完整 context window，并单独标出压缩阈值。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])

    app.ui.show_context_usage(
        estimated_tokens=1234,
        budget_limit=800_000,
        context_window_tokens=1_000_000,
        compacted=False,
    )

    rendered = str(app._formatted_status())
    assert "ctx 1.2k/1M 0% · compact at 800k" in rendered


def test_tui_status_uses_budget_when_context_window_is_unknown() -> None:
    """未知模型没有完整窗口时，TUI 继续按压缩预算展示。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])

    app.ui.show_context_usage(
        estimated_tokens=1234,
        budget_limit=10_000,
        context_window_tokens=None,
        compacted=False,
    )

    rendered = str(app._formatted_status())
    assert "ctx 1.2k/10k 12%" in rendered


def test_tui_status_marks_compacted_context_and_clear_resets_usage() -> None:
    """发生压缩时 status 应标记 compacted，清空上下文后应移除用量展示。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])

    app.ui.show_context_usage(
        estimated_tokens=9500,
        budget_limit=10_000,
        context_window_tokens=None,
        compacted=True,
    )
    assert "compacted" in str(app._formatted_status())

    app.ui.show_context_cleared()

    assert "ctx " not in str(app._formatted_status())


def test_tui_tool_result_finishes_active_streaming_message() -> None:
    """工具调用后的下一轮最终答复不应覆盖上一轮 streaming 前言。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])

    with app.ui.stream_assistant_message() as write_assistant:
        write_assistant("I will inspect first.")
    app.ui.show_tool_result(
        "bash",
        '{"command": "ls"}',
        ToolResult(success=True, summary="$ ls", detail=""),
    )
    app.ui.show_assistant_message("Final answer")

    assert "I will inspect first." in app.transcript_text
    assert "Final answer" in app.transcript_text


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
    """transcript 超过一屏时，控件只渲染底部可视页，避免内部滚动状态抢控制。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "\n".join(f"line {index}" for index in range(60)))

    content = app.transcript_control.create_content(width=80, height=20)

    assert content.cursor_position.y == 19
    assert content.line_count == 20
    assert _content_line_text(content, 0) == "line 40"
    assert _content_line_text(content, content.line_count - 1) == "line 59"


def test_transcript_manual_scroll_changes_visible_slice() -> None:
    """PageUp/滚轮滚动应改变 transcript 视图，而不是被默认窗口滚动重置。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "\n".join(f"line {index}" for index in range(60)))
    app.transcript_control.create_content(width=80, height=20)

    app.scroll_transcript(20)
    content = app.transcript_control.create_content(width=80, height=20)

    assert content.cursor_position.y == 19
    assert content.line_count == 20
    assert _content_line_text(content, 0) == "line 20"
    assert _content_line_text(content, content.line_count - 1) == "line 39"


def test_transcript_manual_scroll_stays_pinned_when_new_output_arrives() -> None:
    """手动离开底部后，新输出不应把当前历史视图继续向下推。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "\n".join(f"line {index}" for index in range(60)))
    app.transcript_control.create_content(width=80, height=20)
    app.scroll_transcript(20)
    before = app.transcript_control.create_content(width=80, height=20)

    app.append_item("assistant", "assistant", "line 60")
    after = app.transcript_control.create_content(width=80, height=20)

    assert _content_line_text(before, before.line_count - 1) == "line 39"
    assert _content_line_text(after, after.line_count - 1) == "line 39"
    assert app.status.new_output is True
    assert not app._follow_bottom


def test_transcript_returning_to_bottom_resumes_following_new_output() -> None:
    """手动滚动回到底部后，应清除新输出标记并恢复自动跟随。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "\n".join(f"line {index}" for index in range(60)))
    app.transcript_control.create_content(width=80, height=20)
    app.scroll_transcript(20)
    app.append_item("assistant", "assistant", "line 60")

    app.scroll_transcript(-1000)
    bottom = app.transcript_control.create_content(width=80, height=20)
    app.append_item("assistant", "assistant", "line 61")
    after = app.transcript_control.create_content(width=80, height=20)

    assert _content_line_text(bottom, bottom.line_count - 1) == "line 60"
    assert _content_line_text(after, after.line_count - 1) == "line 61"
    assert app.status.new_output is False
    assert app._follow_bottom


def test_transcript_submit_after_long_output_stays_at_bottom() -> None:
    """长 transcript 后继续输入，应直接恢复底部跟随且滚动条停在最下方。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "\n".join(f"line {index}" for index in range(60)))
    app.transcript_control.create_content(width=80, height=20)
    app.scroll_transcript(20)

    app.submit_text("next")
    app.append_item("user", "user", "next")
    content = app.transcript_control.create_content(width=80, height=20)
    state = app._scrollbar_state_locked()

    assert _content_line_text(content, content.line_count - 1) == "next"
    assert state.thumb_top + state.thumb_height == state.track_height
    assert app._follow_bottom
    assert app._scroll_offset == 0


def test_transcript_scrollbar_is_hidden_when_content_fits() -> None:
    """内容不足一屏时，右侧滚动条只占位不显示滑块。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "\n".join(f"line {index}" for index in range(10)))

    app.transcript_control.create_content(width=80, height=20)
    rendered = "".join(text for _style, text in app._formatted_scrollbar())

    assert "█" not in rendered
    assert "│" not in rendered


def test_transcript_scrollbar_thumb_tracks_bottom_position() -> None:
    """长 transcript 应显示按比例计算的滚动条滑块。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "\n".join(f"line {index}" for index in range(60)))

    app.transcript_control.create_content(width=80, height=20)
    state = app._scrollbar_state_locked()

    assert state.visible
    assert state.thumb_height == 6
    assert state.thumb_top == 14
    assert state.max_scroll_offset == 40


def test_transcript_scrollbar_probe_render_does_not_shrink_viewport() -> None:
    """scrollbar 自身的布局探测不应覆盖 transcript 的真实可视高度。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "\n".join(f"line {index}" for index in range(60)))
    app.transcript_control.create_content(width=80, height=20)

    app.scrollbar_control.create_content(width=1, height=1)
    state = app._scrollbar_state_locked()

    assert state.track_height == 20
    assert state.thumb_height == 6
    assert state.thumb_top == 14


def test_transcript_scrollbar_track_click_jumps_to_position() -> None:
    """点击滚动条轨道应把 transcript 跳转到对应历史位置。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "\n".join(f"line {index}" for index in range(60)))
    app.transcript_control.create_content(width=80, height=20)

    app.scrollbar_control.mouse_handler(_mouse_event(0, MouseEventType.MOUSE_DOWN))

    assert app._scroll_offset == 40
    assert not app._follow_bottom


def test_transcript_scrollbar_drag_updates_offset_and_returns_to_bottom() -> None:
    """拖动滚动条滑块应实时更新位置，拖到底部后恢复 follow-bottom。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "\n".join(f"line {index}" for index in range(60)))
    app.transcript_control.create_content(width=80, height=20)

    app.scrollbar_control.mouse_handler(_mouse_event(14, MouseEventType.MOUSE_DOWN))
    app.scrollbar_control.mouse_handler(_mouse_event(0, MouseEventType.MOUSE_MOVE))
    assert app._scroll_offset == 40
    assert not app._follow_bottom

    app.scrollbar_control.mouse_handler(_mouse_event(14, MouseEventType.MOUSE_MOVE))
    app.scrollbar_control.mouse_handler(_mouse_event(14, MouseEventType.MOUSE_UP))

    assert app._scroll_offset == 0
    assert app._follow_bottom


def test_transcript_scrollbar_stops_dragging_on_plain_mouse_move() -> None:
    """释放事件丢失后，普通鼠标移动不应继续拖动滚动条。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    app.append_item("assistant", "assistant", "\n".join(f"line {index}" for index in range(60)))
    app.transcript_control.create_content(width=80, height=20)

    app.scrollbar_control.mouse_handler(_mouse_event(14, MouseEventType.MOUSE_DOWN))
    app.scrollbar_control.mouse_handler(_mouse_event(14, MouseEventType.MOUSE_MOVE))
    assert app._scroll_offset == 0

    app.scrollbar_control.mouse_handler(
        _mouse_event(0, MouseEventType.MOUSE_MOVE, button=MouseButton.NONE)
    )

    assert app._scroll_offset == 0
    assert app._follow_bottom
    assert app._scrollbar_drag_offset is None


def test_tui_working_status_records_tool_start() -> None:
    """工具开始执行时应进入 transcript，并同步更新状态栏。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])

    with app.ui.show_working("正在执行工具：bash", timeout_seconds=30):
        assert "正在执行工具：bash" in app.status.label

    assert "running 正在执行工具：bash" in app.transcript_text
