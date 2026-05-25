"""dong CLI 入口端到端测试：覆盖单次 prompt 和 REPL 命令路径。"""

from __future__ import annotations

import sys
import subprocess
from io import StringIO
from types import SimpleNamespace

import pytest

from dong import cli


def _assistant_message(content: str = "", tool_calls: list | None = None):
    """构造模拟的 assistant 消息，避免测试依赖真实 LLM。"""
    return SimpleNamespace(content=content, tool_calls=tool_calls or [])


def _tool_call(call_id: str, name: str, arguments: str):
    """构造模拟 tool_call，驱动 CLI 执行指定工具。"""
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def test_single_prompt_mode_runs_through_tool_call_and_final_answer(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """单次 prompt 模式应完成工具调用并输出最终回答。"""
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
    """REPL 模式应保留 clear、skill 加载、unskill 和 exit 命令行为。"""
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


def test_python_directory_entrypoint_does_not_shadow_stdlib_logging(tmp_path) -> None:
    """`python dong` 目录入口不应让项目 Module 遮蔽标准库 logging。"""
    result = subprocess.run(
        [sys.executable, "dong", "-d", str(tmp_path)],
        input="exit\n",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "partially initialized module 'logging'" not in result.stderr
    assert "dong" in result.stderr
