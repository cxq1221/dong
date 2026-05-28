"""REPL 命令处理的行为兼容测试。"""

from __future__ import annotations

from io import StringIO
from threading import Event

from dong import cli
from dong.cli import (
    _run_repl_sync,
    _run_repl_with_input_queue,
    handle_repl_command,
    repl_completions,
)
from dong.contract import ContractController
from dong.ocr import OcrLine, OcrResult, image_marker
from dong.tui import TuiApp
from dong.ui import TerminalUI


def _write(path, content: str) -> None:
    """写入测试文件，并自动创建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _ui() -> tuple[TerminalUI, StringIO]:
    """构造只捕获 stderr 的测试 UI。"""
    err = StringIO()
    return TerminalUI(stderr=err), err


class QueueTestUI(TerminalUI):
    """用于模拟交互式输入队列的测试 UI。"""

    def __init__(
        self,
        *,
        first_started: Event,
        release_first: Event,
        second_started: Event,
    ) -> None:
        super().__init__(stdout=StringIO(), stderr=StringIO())
        self.first_started = first_started
        self.release_first = release_first
        self.second_started = second_started
        self.read_count = 0
        self.queued: list[int] = []
        self.prompts: list[str] = []

    def read_prompt(self, completions=(), *, prompt_text="\ndong ", bottom_toolbar=None, workdir=None):  # type: ignore[no-untyped-def]
        """第一条执行中输入第二条，第二条开始后再退出。"""
        self.prompts.append(prompt_text)
        self.read_count += 1
        if self.read_count == 1:
            return "first"
        if self.read_count == 2:
            assert self.first_started.wait(timeout=1)
            return "second"
        assert self.first_started.is_set()
        self.release_first.set()
        assert self.second_started.wait(timeout=1)
        return "exit"

    def show_input_queued(self, *, pending: int) -> None:
        self.queued.append(pending)


def test_clear_command_preserves_behavior() -> None:
    """clear 命令应清空工作上下文并返回已处理状态。"""
    ui, err = _ui()
    working = [{"role": "user", "content": "hello"}]

    action = handle_repl_command(
        "clear",
        workdir="/tmp",
        loaded_skills=[],
        working=working,
        ui=ui,
    )

    assert action.handled is True
    assert action.exit_requested is False
    assert working == []
    assert "context cleared" in err.getvalue()


def test_clear_command_updates_session_snapshot(tmp_path) -> None:
    """clear 命令接入 session 时，应同步清空可恢复上下文。"""
    from dong.session import SessionStore

    ui, _err = _ui()
    session = SessionStore(str(tmp_path)).create()
    working = session.messages
    session.append_message({"role": "user", "content": "hello"})

    action = handle_repl_command(
        "clear",
        workdir=str(tmp_path),
        loaded_skills=[],
        working=working,
        ui=ui,
        session=session,
    )
    loaded = SessionStore(str(tmp_path)).load(session.session_id)

    assert action.handled is True
    assert working == []
    assert loaded.messages == []


def test_ocr_command_builds_prompt_from_image_text(tmp_path, monkeypatch) -> None:
    """`/ocr` 应把本地识别结果转换成普通文本 prompt 继续进入模型。"""
    image_path = tmp_path / "screen shot.png"
    image_path.write_bytes(b"fake image")
    ui, _err = _ui()

    def fake_extract(path: str) -> OcrResult:
        assert path == str(image_path)
        return OcrResult(
            image_path=str(image_path),
            lines=(OcrLine("错误日志第一行", 0.99), OcrLine("错误日志第二行", 0.98)),
        )

    monkeypatch.setattr(cli, "extract_text_from_image", fake_extract)

    action = handle_repl_command(
        f'/ocr "{image_path}" 这是什么问题',
        workdir=str(tmp_path),
        loaded_skills=[],
        working=[],
        ui=ui,
    )

    assert action.handled is True
    assert action.prompt is not None
    assert "不要假装直接看到了原图" in action.prompt
    assert "错误日志第一行" in action.prompt
    assert "用户输入" in action.prompt
    assert "这是什么问题" in action.prompt


def test_ocr_command_reports_usage_without_image_path(tmp_path) -> None:
    """`/ocr` 缺少图片路径时不应进入模型请求。"""
    ui, err = _ui()

    action = handle_repl_command(
        "/ocr",
        workdir=str(tmp_path),
        loaded_skills=[],
        working=[],
        ui=ui,
    )

    assert action.handled is True
    assert action.prompt is None
    assert "Usage: /ocr <image-path> [question]" in err.getvalue()


def test_repl_image_marker_expands_to_ocr_prompt(tmp_path, monkeypatch) -> None:
    """输入框图片占位符提交时应展开为 OCR 文本再进入模型。"""
    ui, _err = _ui()
    working: list[dict[str, str]] = []
    image_path = tmp_path / "clipboard.png"
    image_path.write_bytes(b"fake png")

    def fake_extract(path: str) -> OcrResult:
        assert path == str(image_path)
        return OcrResult(
            image_path=path,
            lines=(OcrLine("图片中的报错"),),
        )

    def fake_run_loop(base_sys, working, _workdir, *, max_turns, ui, enable_mcp):  # type: ignore[no-untyped-def]
        working.append({"role": "assistant", "content": "ok"})

    monkeypatch.setattr("dong.ocr.extract_text_from_image", fake_extract)
    monkeypatch.setattr(cli, "run_loop", fake_run_loop)

    cli._process_repl_input(
        f"{image_marker(image_path)} 怎么修",
        workdir=str(tmp_path),
        loaded_skills=[],
        working=working,
        ui=ui,
        max_turns=3,
        enable_mcp=False,
    )

    assert "图片中的报错" in working[0]["content"]
    assert "怎么修" in working[0]["content"]


def test_compact_command_forces_context_summary(tmp_path) -> None:
    """`/compact` 应立即压缩旧上下文并保留最近消息。"""
    ui, err = _ui()
    working = [
        {"role": "user", "content": f"message {index}"}
        for index in range(7)
    ]

    action = handle_repl_command(
        "/compact",
        workdir=str(tmp_path),
        loaded_skills=[],
        working=working,
        ui=ui,
    )

    assert action.handled is True
    assert working[0]["role"] == "system"
    assert "Compacted conversation context" in working[0]["content"]
    assert "message 0" in working[0]["content"]
    assert len(working) == 5
    assert list((tmp_path / ".dong" / "context").glob("compact-*.md"))
    rendered = err.getvalue()
    assert "context compacted" in rendered
    assert "removed=3" in rendered


def test_compact_command_updates_session_snapshot(tmp_path) -> None:
    """`/compact` 接入 session 时，应同步保存压缩后的上下文和 metadata。"""
    from dong.session import SessionStore

    ui, _err = _ui()
    store = SessionStore(str(tmp_path))
    session = store.create(model="deepseek-v4-pro")
    for index in range(7):
        session.append_message({"role": "user", "content": f"message {index}"})
    working = session.messages

    action = handle_repl_command(
        "/compact",
        workdir=str(tmp_path),
        loaded_skills=[],
        working=working,
        ui=ui,
        session=session,
    )
    loaded = store.load(session.session_id)

    assert action.handled is True
    assert loaded.messages[0]["role"] == "system"
    assert loaded.compactions[-1]["removed_messages"] == 3


def test_compact_command_noops_when_history_is_too_short(tmp_path) -> None:
    """旧上下文不足时，`/compact` 不应生成空摘要。"""
    ui, err = _ui()
    working = [{"role": "user", "content": "only current request"}]

    action = handle_repl_command(
        "/compact",
        workdir=str(tmp_path),
        loaded_skills=[],
        working=working,
        ui=ui,
    )

    assert action.handled is True
    assert working == [{"role": "user", "content": "only current request"}]
    assert not (tmp_path / ".dong" / "context").exists()
    assert "not enough old context" in err.getvalue()


def test_contract_commands_update_controller(tmp_path) -> None:
    """`/contract on|off|status` 应控制当前契约压力层。"""
    ui, err = _ui()
    controller = ContractController(workdir=str(tmp_path))

    on = handle_repl_command(
        "/contract on",
        workdir=str(tmp_path),
        loaded_skills=[],
        working=[],
        ui=ui,
        contract_controller=controller,
    )
    status = handle_repl_command(
        "/contract status",
        workdir=str(tmp_path),
        loaded_skills=[],
        working=[],
        ui=ui,
        contract_controller=controller,
    )
    off = handle_repl_command(
        "/contract off",
        workdir=str(tmp_path),
        loaded_skills=[],
        working=[],
        ui=ui,
        contract_controller=controller,
    )

    assert on.handled is True
    assert status.handled is True
    assert off.handled is True
    rendered = err.getvalue()
    assert "contract mode: on" in rendered
    assert "pressure level" in rendered
    assert "contract mode: off" in rendered


def test_contract_controller_follows_repl_workdir_change(tmp_path) -> None:
    """切换工作目录时，契约控制器也应跟随新的工作区。"""
    next_workdir = tmp_path / "next"
    next_workdir.mkdir()
    ui, _err = _ui()
    controller = ContractController(workdir=str(tmp_path))

    action = handle_repl_command(
        f"dir={next_workdir}",
        workdir=str(tmp_path),
        loaded_skills=[],
        working=[],
        ui=ui,
        contract_controller=controller,
    )

    assert action.handled is True
    assert action.workdir == str(next_workdir.resolve())
    assert controller.workdir == str(next_workdir.resolve())


def test_interactive_repl_queues_input_while_agent_is_working(
    tmp_path,
    monkeypatch,
) -> None:
    """AI 工作中输入的新消息应排队，并在当前轮结束后自动执行。"""
    first_started = Event()
    release_first = Event()
    second_started = Event()
    seen_prompts: list[str] = []
    ui = QueueTestUI(
        first_started=first_started,
        release_first=release_first,
        second_started=second_started,
    )

    def fake_run_loop(_base_sys, working, _workdir, *, max_turns, ui, enable_mcp):  # type: ignore[no-untyped-def]
        prompt = working[-1]["content"]
        seen_prompts.append(prompt)
        if prompt == "first":
            first_started.set()
            assert release_first.wait(timeout=1)
        if prompt == "second":
            second_started.set()
        working.append({"role": "assistant", "content": f"done {prompt}"})

    monkeypatch.setattr(cli, "run_loop", fake_run_loop)

    _run_repl_with_input_queue(
        workdir=str(tmp_path),
        loaded_skills=[],
        working=[],
        ui=ui,
        max_turns=3,
        enable_mcp=False,
    )

    assert seen_prompts == ["first", "second"]
    assert ui.prompts[:2] == ["\ndong ", "\ndong next "]
    assert ui.queued == [1]


def test_dir_command_returns_new_absolute_workdir(tmp_path) -> None:
    """dir= 命令应返回新的绝对工作目录。"""
    ui, err = _ui()

    action = handle_repl_command(
        f"dir={tmp_path}",
        workdir="/tmp",
        loaded_skills=[],
        working=[],
        ui=ui,
    )

    assert action.handled is True
    assert action.workdir == str(tmp_path)
    assert "workdir" in err.getvalue()
    assert tmp_path.name in err.getvalue()


def test_dir_command_rejects_missing_workdir(tmp_path) -> None:
    """dir= 不应把 REPL 状态切到不存在的目录。"""
    ui, err = _ui()
    missing = tmp_path / "missing"

    action = handle_repl_command(
        f"dir={missing}",
        workdir=str(tmp_path),
        loaded_skills=[],
        working=[],
        ui=ui,
    )

    assert action.handled is True
    assert action.workdir is None
    rendered = err.getvalue()
    assert "Error" in rendered
    assert missing.name in rendered


def test_skill_load_and_unskill_preserve_loaded_skill_list(tmp_path) -> None:
    """skill 加载和移除应正确维护 loaded_skills 列表。"""
    _write(tmp_path / ".dong" / "skills" / "review.md", "# Review")
    ui, err = _ui()
    loaded: list[str] = []

    load_action = handle_repl_command(
        "/skill review",
        workdir=str(tmp_path),
        loaded_skills=loaded,
        working=[],
        ui=ui,
    )
    remove_action = handle_repl_command(
        "/unskill review",
        workdir=str(tmp_path),
        loaded_skills=loaded,
        working=[],
        ui=ui,
    )

    assert load_action.handled is True
    assert remove_action.handled is True
    assert loaded == []
    rendered = err.getvalue()
    assert "Loaded skill" in rendered
    assert "Removed skill: review" in rendered


def test_slash_root_lists_supported_commands(tmp_path) -> None:
    """输入 `/` 应展示当前支持的用户级 slash 命令。"""
    ui, err = _ui()

    action = handle_repl_command(
        "/",
        workdir=str(tmp_path),
        loaded_skills=[],
        working=[],
        ui=ui,
    )

    assert action.handled is True
    rendered = err.getvalue()
    assert "/skill" in rendered
    assert "/sessions" in rendered
    assert "/ocr" in rendered
    assert "/compact" in rendered


def test_sessions_command_lists_current_workspace_sessions(tmp_path) -> None:
    """`/sessions` 应列出当前工作区可恢复的 session。"""
    from dong.session import SessionStore

    store = SessionStore(str(tmp_path))
    first = store.create(model="deepseek-v4-pro")
    first.append_message({"role": "user", "content": "older"})
    second = store.create(model="deepseek-v4-pro")
    second.append_message({"role": "user", "content": "newer"})
    ui, err = _ui()

    action = handle_repl_command(
        "/sessions",
        workdir=str(tmp_path),
        loaded_skills=[],
        working=second.messages,
        ui=ui,
        session=second,
    )

    assert action.handled is True
    rendered = err.getvalue()
    assert first.session_id in rendered
    assert second.session_id in rendered
    assert "messages" in rendered
    assert "*" in rendered


def test_sessions_command_can_switch_session_from_selector(tmp_path) -> None:
    """TUI selector 选择 session 后，应切换当前 REPL working 上下文。"""
    from dong.session import SessionStore

    class SelectorUI(TerminalUI):
        """模拟 TUI 的 session 选择能力。"""

        def __init__(self, selected_id: str) -> None:
            super().__init__(stderr=StringIO())
            self.selected_id = selected_id
            self.restored: object | None = None

        def select_session(self, summaries, *, current_session_id=None):  # type: ignore[no-untyped-def]
            return self.selected_id

        def show_session_restored(self, result) -> None:  # type: ignore[no-untyped-def]
            self.restored = result

    store = SessionStore(str(tmp_path))
    first = store.create(model="deepseek-v4-pro")
    first.append_message({"role": "user", "content": "first context"})
    second = store.create(model="deepseek-v4-pro")
    second.append_message({"role": "user", "content": "second context"})
    working = second.messages
    ui = SelectorUI(first.session_id)

    action = handle_repl_command(
        "/sessions",
        workdir=str(tmp_path),
        loaded_skills=[],
        working=working,
        ui=ui,
        session=second,
    )

    assert action.handled is True
    assert action.session is not None
    assert action.session.session_id == first.session_id
    assert working == [{"role": "user", "content": "first context"}]
    assert action.session.messages is working
    assert ui.restored is not None
    assert getattr(ui.restored, "session").session_id == first.session_id
    assert first.session_id in getattr(ui.restored, "resume_command")


def test_exit_prints_copyable_resume_command(tmp_path) -> None:
    """退出 REPL session 时应打印完整可复制恢复命令。"""
    from dong.session import SessionStore

    session = SessionStore(str(tmp_path)).create(model="deepseek-v4-pro")
    ui, err = _ui()

    action = handle_repl_command(
        "exit",
        workdir=str(tmp_path),
        loaded_skills=[],
        working=session.messages,
        ui=ui,
        session=session,
    )

    assert action.handled is True
    assert action.exit_requested is True
    rendered = err.getvalue()
    assert "Resume this session" in rendered
    assert f"dong -d {tmp_path} --resume {session.session_id}" in rendered


def test_ctrl_c_prompt_exit_prints_copyable_resume_command(tmp_path) -> None:
    """提示符处 Ctrl-C 退出时应直接打印恢复命令。"""
    from dong.session import SessionStore

    session = SessionStore(str(tmp_path)).create(model="deepseek-v4-pro")
    err = StringIO()

    def raise_keyboard_interrupt(_prompt: str) -> str:
        raise KeyboardInterrupt

    ui = TerminalUI(stderr=err, input_func=raise_keyboard_interrupt)

    _run_repl_sync(
        workdir=str(tmp_path),
        loaded_skills=[],
        working=session.messages,
        ui=ui,
        max_turns=3,
        enable_mcp=False,
        session=session,
    )

    rendered = err.getvalue()
    assert "Resume this session" in rendered
    assert f"dong -d {tmp_path} --resume {session.session_id}" in rendered


def test_slash_skill_invocation_returns_prompt(tmp_path) -> None:
    """`/review prompt` 应加载 skill 并把剩余文本作为 prompt 返回。"""
    _write(tmp_path / ".dong" / "skills" / "review.md", "# Review")
    ui, err = _ui()
    loaded: list[str] = []

    action = handle_repl_command(
        "/review inspect cli.py",
        workdir=str(tmp_path),
        loaded_skills=loaded,
        working=[],
        ui=ui,
    )

    assert action.handled is True
    assert action.prompt == "inspect cli.py"
    assert loaded == ["review"]
    rendered = err.getvalue()
    assert "Matched skill" in rendered
    assert "review" in rendered
    assert "slash" in rendered


def test_bare_skill_invocation_supports_frontmatter_alias(tmp_path) -> None:
    """裸 skill 调用应支持文件名别名，并按 frontmatter 名称记录已加载项。"""
    _write(
        tmp_path / ".dong" / "skills" / "review.md",
        "---\nname: code-review\n---\n\n# Review",
    )
    ui, err = _ui()
    loaded: list[str] = []

    action = handle_repl_command(
        "review inspect cli.py",
        workdir=str(tmp_path),
        loaded_skills=loaded,
        working=[],
        ui=ui,
    )

    assert action.handled is True
    assert action.prompt == "inspect cli.py"
    assert loaded == ["code-review"]
    rendered = err.getvalue()
    assert "Matched skill" in rendered
    assert "code-review" in rendered
    assert "bare" in rendered


def test_repl_completions_include_commands_and_skills(tmp_path) -> None:
    """REPL 自动补全应包含固定命令、skill 加载和卸载候选。"""
    _write(tmp_path / ".dong" / "skills" / "review.md", "# Review")

    completions = repl_completions(str(tmp_path), ["review"])

    assert "clear" in completions
    assert "/compact" in completions
    assert "/sessions" in completions
    assert "/ocr" in completions
    assert "/skill review" in completions
    assert "/review" in completions
    assert "/unskill review" in completions


def test_repl_completions_include_contract_commands(tmp_path) -> None:
    """REPL 补全应暴露契约命令。"""
    completions = repl_completions(str(tmp_path), [])

    assert "/contract" in completions
    assert "/contract on" in completions
    assert "/contract off" in completions
    assert "/contract status" in completions


def test_repl_completions_include_local_skill_directories(tmp_path) -> None:
    """REPL 自动补全应包含 .dong/skills/<name>/SKILL.md 目录型 skill。"""
    _write(tmp_path / ".dong" / "skills" / "zoom-out" / "SKILL.md", "# Zoom Out")

    completions = repl_completions(str(tmp_path), [])

    assert "/skill zoom-out" in completions
    assert "/zoom-out" in completions
    assert "zoom-out" in completions


def test_repl_completions_include_frontmatter_name_and_entry_alias(tmp_path) -> None:
    """REPL 自动补全应包含规范 skill 名和路径别名快捷项。"""
    _write(
        tmp_path / ".dong" / "skills" / "review.md",
        "---\nname: code-review\n---\n\n# Review",
    )

    completions = repl_completions(str(tmp_path), ["code-review"])

    assert "/skill code-review" in completions
    assert "/code-review" in completions
    assert "/review" in completions


def test_repl_prompt_does_not_preselect_skill(tmp_path, monkeypatch) -> None:
    """普通 prompt 不再做确定性预选，skill 交给模型通过工具发现和加载。"""

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    _write(
        tmp_path / ".dong" / "skills" / "chrome-cdp" / "SKILL.md",
        (
            "---\n"
            "name: chrome-cdp\n"
            "description: Inspect local Chrome pages\n"
            "keywords: 浏览器, 当前页面\n"
            "---\n\n"
            "# Chrome CDP\n"
        ),
    )
    ui, err = _ui()
    loaded: list[str] = []
    seen_instructions: list[str] = []

    def fake_run_loop(base_sys, working, _workdir, *, max_turns, ui, enable_mcp):  # type: ignore[no-untyped-def]
        seen_instructions.append(base_sys.instructions)
        working.append({"role": "assistant", "content": "ok"})

    monkeypatch.setattr(cli, "run_loop", fake_run_loop)

    cli._process_repl_input(
        "帮我看一下当前浏览器页面",
        workdir=str(tmp_path),
        loaded_skills=loaded,
        working=[],
        ui=ui,
        max_turns=3,
        enable_mcp=False,
    )

    assert loaded == []
    assert "Skill: chrome-cdp" not in seen_instructions[0]
    assert "Matched skill" not in err.getvalue()


def test_repl_prompt_does_not_auto_inject_skill_in_tui(tmp_path, monkeypatch) -> None:
    """fullscreen TUI 下普通 prompt 同样不做运行时自动 skill 预选。"""

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    _write(
        tmp_path / ".dong" / "skills" / "chrome-cdp" / "SKILL.md",
        "---\nname: chrome-cdp\nkeywords: 浏览器\n---\n\n# Chrome CDP\n",
    )
    loaded: list[str] = []
    seen_instructions: list[str] = []
    app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])

    def fake_run_loop(base_sys, working, _workdir, *, max_turns, ui, enable_mcp):  # type: ignore[no-untyped-def]
        seen_instructions.append(base_sys.instructions)
        working.append({"role": "assistant", "content": "ok"})

    monkeypatch.setattr(cli, "run_loop", fake_run_loop)

    cli._process_repl_input(
        "帮我看浏览器",
        workdir=str(tmp_path),
        loaded_skills=loaded,
        working=[],
        ui=app.ui,
        max_turns=3,
        enable_mcp=False,
    )

    assert "Skill: chrome-cdp" not in seen_instructions[0]
    assert "Matched skill: chrome-cdp" not in app.transcript_text
