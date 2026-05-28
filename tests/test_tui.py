"""Inline TUI behavior tests."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from prompt_toolkit.document import Document
from prompt_toolkit.keys import Keys

from dong.ocr import image_marker
from dong.session import Session
from dong.session_recovery import SessionRestoreResult, session_transcript_preview
from dong.tool import ToolResult
from dong.tui import TuiApp, _fit_to_width, render_markdown, render_text
from dong.ui import _cancel_shortcut_hint, _shortcut_label


def _completion_texts(app: TuiApp, text: str) -> list[str]:
    completer = app.composer.completer
    assert completer is not None
    return [item.text for item in completer.get_completions(Document(text), None)]


def _press_key(app: TuiApp, key: Keys | str) -> None:
    """执行指定 TUI key binding，便于测试复制/取消这类全局按键。"""
    for binding in app.key_bindings.bindings:
        if binding.keys == (key,):
            binding.handler(SimpleNamespace(app=app.application))
            return
    raise AssertionError(f"missing key binding: {key}")


def test_shortcut_labels_follow_platform(monkeypatch) -> None:
    """取消提示应展示终端真实能传入应用的按键。"""
    monkeypatch.setattr("dong.ui.platform.system", lambda: "Darwin")
    assert _shortcut_label("c") == "Cmd-C"
    assert _cancel_shortcut_hint() == "Ctrl-C / Esc-C 取消当前任务"

    monkeypatch.setattr("dong.ui.platform.system", lambda: "Linux")
    assert _shortcut_label("c") == "Ctrl-C"
    assert _cancel_shortcut_hint() == "Ctrl-C 取消当前任务"


def test_tui_macos_shortcuts_accept_escape_aliases(monkeypatch) -> None:
    """macOS 下除 Ctrl 触发外，也接受终端能传入的 Esc+key 兼容序列。"""
    monkeypatch.setattr("dong.ui.platform.system", lambda: "Darwin")

    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    keys = {binding.keys for binding in app.key_bindings.bindings}

    assert (Keys.ControlC,) in keys
    assert (Keys.Escape, "c") in keys
    assert (Keys.Escape, "d") in keys


def test_tui_ctrl_c_sets_cooperative_cancel_when_busy() -> None:
    """TUI 忙碌时 Ctrl-C 应设置可被 worker/LLM 读取的取消事件。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])

    with app._lock:
        app._busy = True

    _press_key(app, Keys.ControlC)

    assert app.status.cancellation_requested
    assert app.cancel_requested()
    assert app.ui.cancel_requested()


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
    deadline = time.monotonic() + 1
    while not app.composer.buffer.read_only() and time.monotonic() < deadline:
        time.sleep(0.01)
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
        target=lambda: result.append(app.ui.select_session(items)),
        daemon=True,
    )

    thread.start()
    assert app._session_picker is not None
    assert "old prompt" in str(app._formatted_live_transcript())

    assert app.move_session_picker_selection(1) is True
    assert app.accept_session_picker() is True
    thread.join(timeout=1)

    assert result == ["session-new"]
    assert app._session_picker is None
    app.ui.show_session_restored(
        SessionRestoreResult(
            session=Session(
                session_id="session-new",
                created_at_ms=1,
                updated_at_ms=1,
                workspace_root=".",
                messages=[
                    {"role": "user", "content": "restored user question"},
                    {"role": "assistant", "content": "restored assistant answer"},
                ],
            ),
            resume_command="dong -d . --resume session-new",
            transcript_preview=session_transcript_preview([
                {"role": "user", "content": "restored user question"},
                {"role": "assistant", "content": "restored assistant answer"},
            ]),
        )
    )
    assert "Restored session: session-new" in app.transcript_text
    assert "Context loaded. Continue typing." in app.transcript_text
    assert "restored user question" in app.transcript_text
    assert "restored assistant answer" in app.transcript_text
    assert "Recent session content" not in app.transcript_text
    assert "Selected session:" not in app.transcript_text
    assert "old prompt" not in app.transcript_text


def test_tui_session_picker_loads_more_sessions_when_scrolling_down() -> None:
    """历史 session 很多时，选择器默认只展示 10 个，向下移动才加载更多。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
    items = [
        SimpleNamespace(
            session_id=f"session-{index:02d}",
            updated_at_ms=1000 + index,
            message_count=index,
            prompt_preview=f"prompt {index:02d}",
            assistant_preview=f"answer {index:02d}",
        )
        for index in range(25)
    ]
    thread = threading.Thread(
        target=lambda: app.ui.select_session(items),
        daemon=True,
    )

    thread.start()
    assert app._session_picker is not None
    live_text = str(app._formatted_live_transcript())
    assert "prompt 09" in live_text
    assert "prompt 10" not in live_text
    assert "15 remaining" in live_text

    for _ in range(10):
        assert app.move_session_picker_selection(1)

    live_text = str(app._formatted_live_transcript())
    assert "prompt 10" in live_text
    assert "prompt 19" in live_text
    assert "prompt 20" not in live_text
    assert "5 remaining" in live_text

    assert app.cancel_session_picker()
    thread.join(timeout=1)


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
    assert "› /skill\n    ----------\n    /review" in rendered
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
    assert "  /skill\n    ----------\n  › /python-test" in rendered
    assert app.composer.text == "/"

    assert app.accept_completion_selection()
    assert app.composer.text == "/python-test"


def test_tui_skill_command_opens_skill_name_menu_without_trailing_space() -> None:
    """输入精确 /skill 或 /skills 时应直接弹出 skill 名列表，并用上下键循环选择。"""
    app = TuiApp(
        process_input=lambda _text, _ui: False,
        completion_provider=lambda: ["/skill", "/skill review", "/skill python-test"],
    )
    app.submit_text("previous user message")
    app.composer.buffer.text = "/skill"

    rendered = "".join(text for _style, text in app._formatted_completion_menu())
    assert "› python-test\n    review" in rendered

    assert app.move_completion_selection(-1)
    rendered = "".join(text for _style, text in app._formatted_completion_menu())
    assert "  python-test\n  › review" in rendered
    assert app.composer.text == "/skill"

    assert app.accept_completion_selection()
    assert app.composer.text == "/skill review"

    app.composer.buffer.text = "/skills"
    rendered = "".join(text for _style, text in app._formatted_completion_menu())
    assert "› python-test\n    review" in rendered

    assert app.accept_completion_selection()
    assert app.composer.text == "/skill python-test"


def test_tui_skill_menu_can_reach_late_sorted_skills() -> None:
    """skill 很多时菜单只显示小窗口，但上下键必须能走到字母靠后的 skill。"""
    skill_names = [f"skill-{index:02d}" for index in range(12)] + ["zoom-out"]
    app = TuiApp(
        process_input=lambda _text, _ui: False,
        completion_provider=lambda: ["/skill", *[f"/skill {name}" for name in skill_names]],
    )
    app.composer.buffer.text = "/skill"

    rendered = "".join(text for _style, text in app._formatted_completion_menu())
    assert "zoom-out" not in rendered

    for _ in range(len(skill_names) - 1):
        assert app.move_completion_selection(1)

    rendered = "".join(text for _style, text in app._formatted_completion_menu())
    assert "› zoom-out" in rendered

    assert app.accept_completion_selection()
    assert app.composer.text == "/skill zoom-out"


def test_tui_slash_sessions_completion_submits_command_directly() -> None:
    """菜单里选中 /sessions 时应直接进入功能，不先填回输入框。"""
    app = TuiApp(
        process_input=lambda _text, _ui: False,
        completion_provider=lambda: ["/sessions"],
    )
    app.composer.buffer.text = "/"

    assert app.accept_completion_selection()

    assert app.composer.text == ""
    assert app._input_queue.get_nowait() == "/sessions"


def test_tui_uses_inline_native_copy_mode() -> None:
    """TUI 不进入全屏、不捕获鼠标，复制由终端原生选区负责。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])

    assert app.application.full_screen is False
    assert app.application.mouse_support() is False
    assert "native copy" in str(app._formatted_status())


def test_tui_commits_stable_items_to_transcript_memory() -> None:
    """稳定块进入 committed transcript；live viewport 不承载历史滚动。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])

    app.append_item("assistant", "assistant", "alpha\nbeta", raw="alpha\nbeta")

    assert app.transcript_text == "alpha\nbeta"
    assert app._live_items == []


def test_tui_streaming_lives_until_context_exit() -> None:
    """流式内容先在 live 区更新，结束后才作为不可变块提交。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])

    with app.ui.stream_assistant_message() as write_assistant:
        write_assistant("hello")
        assert "hello" not in app.transcript_text
        assert "hello" in str(app._formatted_live_transcript())

    assert "hello" in app.transcript_text
    assert app._live_items == []


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


def test_tui_working_status_records_tool_start() -> None:
    """工具开始执行只进入 live/status，不污染 committed transcript。"""
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])

    with app.ui.show_working("正在执行工具：bash", timeout_seconds=30):
        assert "正在执行工具：bash" in app.status.label

    assert "running 正在执行工具：bash" not in app.transcript_text
