"""e2e 测试辅助：集中构造模拟模型消息并驱动真实 CLI 入口。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dong import cli


def assistant_message(
    content: str = "",
    tool_calls: list | None = None,
    reasoning_content: str | None = None,
) -> SimpleNamespace:
    """构造模拟 assistant 消息，用真实 CLI 主入口驱动业务流程。"""
    message = SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls or [])
    if reasoning_content is not None:
        message.reasoning_content = reasoning_content
    return message


def tool_call(call_id: str, name: str, arguments: str) -> SimpleNamespace:
    """构造模拟 tool_call，让 CLI 真实执行内置工具。"""
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def records(workdir: Path) -> list[dict]:
    """读取当前工作区唯一 session JSONL 记录。"""
    files = sorted((workdir / ".dong" / "sessions").glob("*/*.jsonl"))
    assert len(files) == 1
    return [
        json.loads(line)
        for line in files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def message_contents(workdir: Path) -> list[str]:
    """返回 session 中所有 message 的 content，便于断言业务上下文。"""
    return [
        record.get("message", {}).get("content", "")
        for record in records(workdir)
        if record.get("type") == "message"
    ]


def run_dong(
    monkeypatch: pytest.MonkeyPatch,
    workdir: Path,
    args: list[str],
    responses: list[SimpleNamespace],
) -> list[list]:
    """通过 cli.main 跑一次 dong，并记录每次模型请求看到的 messages。"""
    seen_messages: list[list] = []
    iterator = iter(responses)

    def fake_chat(messages, _tools, instructions="", **_kwargs):  # type: ignore[no-untyped-def]
        assert instructions
        seen_messages.append(list(messages))
        return next(iterator)

    workdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "chat", fake_chat)
    monkeypatch.setattr(sys, "argv", ["dong", "-d", str(workdir), *args])
    cli.main()
    return seen_messages
