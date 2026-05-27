"""REPL 命令处理的行为兼容测试。"""

from __future__ import annotations

from io import StringIO
from threading import Event

from dong import cli
from dong.cli import _run_repl_with_input_queue, handle_repl_command, repl_completions
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

    def read_prompt(self, completions=(), *, prompt_text="\ndong ", bottom_toolbar=None):  # type: ignore[no-untyped-def]
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
    assert "/skill review" in completions
    assert "/review" in completions
    assert "/unskill review" in completions


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
