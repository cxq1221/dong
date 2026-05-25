"""REPL 命令处理的行为兼容测试。"""

from __future__ import annotations

from io import StringIO

from dong.cli import handle_repl_command, repl_completions
from dong.ui import TerminalUI


def _write(path, content: str) -> None:
    """写入测试文件，并自动创建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _ui() -> tuple[TerminalUI, StringIO]:
    """构造只捕获 stderr 的测试 UI。"""
    err = StringIO()
    return TerminalUI(stderr=err), err


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
    ui, _err = _ui()
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


def test_repl_completions_include_commands_and_skills(tmp_path) -> None:
    """REPL 自动补全应包含固定命令、skill 加载和卸载候选。"""
    _write(tmp_path / ".dong" / "skills" / "review.md", "# Review")

    completions = repl_completions(str(tmp_path), ["review"])

    assert "clear" in completions
    assert "/skill review" in completions
    assert "/review" in completions
    assert "/unskill review" in completions
