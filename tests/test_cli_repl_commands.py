"""Behavior-compatibility tests for REPL command handling."""

from __future__ import annotations

from io import StringIO

from dong.cli import handle_repl_command, repl_completions
from dong.ui import TerminalUI


def _write(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _ui() -> tuple[TerminalUI, StringIO]:
    err = StringIO()
    return TerminalUI(stderr=err), err


def test_clear_command_preserves_behavior() -> None:
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
    _write(tmp_path / ".dong" / "skills" / "review.md", "# Review")

    completions = repl_completions(str(tmp_path), ["review"])

    assert "clear" in completions
    assert "/skill review" in completions
    assert "/review" in completions
    assert "/unskill review" in completions
