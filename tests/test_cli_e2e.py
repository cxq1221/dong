"""End-to-end tests for the dong CLI entrypoint."""

from __future__ import annotations

import sys
from io import StringIO
from types import SimpleNamespace

import pytest

from dong import cli


def _assistant_message(content: str = "", tool_calls: list | None = None):
    return SimpleNamespace(content=content, tool_calls=tool_calls or [])


def _tool_call(call_id: str, name: str, arguments: str):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def test_single_prompt_mode_runs_through_tool_call_and_final_answer(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    responses = iter([
        _assistant_message(tool_calls=[
            _tool_call("call-1", "read", '{"filepath": "note.txt"}')
        ]),
        _assistant_message(content="Read complete."),
    ])

    monkeypatch.setattr(cli, "chat", lambda _messages, _tools: next(responses))
    monkeypatch.setattr(
        sys,
        "argv",
        ["dong", "-d", str(tmp_path), "inspect", "note"],
    )

    cli.main()

    captured = capsys.readouterr()
    assert "dong" in captured.err
    assert "read(" in captured.err
    assert "note.txt" in captured.err
    assert "Read complete." in captured.out


def test_repl_mode_preserves_clear_skill_and_exit_commands(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".dong" / "skills").mkdir(parents=True)
    (tmp_path / ".dong" / "skills" / "review.md").write_text(
        "# Review\n",
        encoding="utf-8",
    )
    stdin = StringIO("clear\n/skill review\n/unskill review\nexit\n")

    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "argv", ["dong", "-d", str(tmp_path)])

    cli.main()

    captured = capsys.readouterr()
    assert "context cleared" in captured.err
    assert "Loaded skill" in captured.err
    assert "Removed skill: review" in captured.err
