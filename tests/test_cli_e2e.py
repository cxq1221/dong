"""dong CLI 入口端到端测试：覆盖单次 prompt 和 REPL 命令路径。"""

from __future__ import annotations

import sys
import subprocess
from io import StringIO
from types import SimpleNamespace

import pytest

from dong import cli


def test_system_prompt_is_clear_and_json_mode_aware() -> None:
    """系统提示词应避免拼写错误、重复规则，并说明 JSON Output 行为。"""
    assert cli.SYSTEM_PROMPT.startswith("You are dong")
    assert cli.SYSTEM_PROMPT.splitlines()[0] != "ou are dong, a coding agent assistant."
    assert cli.SYSTEM_PROMPT.count("Tool results are structured as JSON") == 1
    assert "When JSON Output is enabled" in cli.SYSTEM_PROMPT


def _assistant_message(
    content: str = "",
    tool_calls: list | None = None,
    reasoning_content: str | None = None,
):
    """构造模拟的 assistant 消息，避免测试依赖真实 LLM。"""
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    if reasoning_content is not None:
        message.reasoning_content = reasoning_content
    return message


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


def test_run_loop_preserves_reasoning_content_after_tool_call(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工具调用后的下一轮请求应保留 DeepSeek thinking 的 reasoning_content。"""
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    first_message = _assistant_message(tool_calls=[
        _tool_call("call-1", "read", '{"filepath": "note.txt"}')
    ])
    first_message.reasoning_content = "I should inspect the file before answering."
    responses = iter([
        first_message,
        _assistant_message(content="Read complete."),
    ])
    seen_messages: list[list] = []

    def fake_chat(messages, _tools):
        seen_messages.append(list(messages))
        return next(responses)

    monkeypatch.setattr(cli, "chat", fake_chat)

    cli.run_loop(
        [{"role": "system", "content": "system"}],
        [{"role": "user", "content": "inspect note"}],
        str(tmp_path),
        max_turns=3,
    )

    second_request = seen_messages[1]
    assert any(
        getattr(message, "reasoning_content", None) == first_message.reasoning_content
        for message in second_request
    )


def test_run_loop_displays_reasoning_content(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """模型返回 reasoning_content 时，CLI 应展示 thinking 区块。"""
    responses = iter([
        _assistant_message(
            content="Final answer.",
            reasoning_content="I should explain the tradeoff first.",
        )
    ])

    monkeypatch.setattr(cli, "chat", lambda _messages, _tools: next(responses))

    cli.run_loop(
        [{"role": "system", "content": "system"}],
        [{"role": "user", "content": "answer"}],
        str(tmp_path),
        max_turns=1,
    )

    captured = capsys.readouterr()
    assert "thinking" in captured.err
    assert "I should explain the tradeoff first." in captured.err
    assert "Final answer." in captured.out


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
