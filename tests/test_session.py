"""session 持久化测试：验证 dong 会话文件可恢复且不会串工作区。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dong.session import (
    MAX_JSON_FIELD_CHARS,
    SessionPersistenceError,
    SessionStore,
    workspace_fingerprint,
)


def _tool_call(call_id: str, name: str, arguments: str) -> SimpleNamespace:
    """构造最小 tool_call 替身，模拟 provider SDK 返回结构。"""
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _records(path) -> list[dict]:
    """读取 JSONL 记录，便于断言 session 文件结构。"""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_session_jsonl_roundtrip_preserves_messages_and_prompt_history(tmp_path) -> None:
    """session 应能从 JSONL 恢复用户、assistant 工具调用和 prompt history。"""
    store = SessionStore(str(tmp_path))
    session = store.create(model="deepseek-v4-pro")

    session.record_prompt("inspect README")
    session.append_message({"role": "user", "content": "inspect README"})
    assistant = SimpleNamespace(
        role="assistant",
        content="I will read it.",
        tool_calls=[_tool_call("call-1", "read", '{"filepath": "README.md"}')],
        reasoning_content="Need file content.",
    )
    session.append_message(assistant)

    loaded = store.load(session.session_id)
    records = _records(session.persistence_path)
    latest_meta = [
        record for record in records if record["type"] == "session_meta"
    ][-1]

    assert loaded.session_id == session.session_id
    assert latest_meta["message_count"] == 2
    assert loaded.prompt_history[0]["text"] == "inspect README"
    assert loaded.messages[0] == {"role": "user", "content": "inspect README"}
    assert loaded.messages[1]["role"] == "assistant"
    assert loaded.messages[1]["reasoning_content"] == "Need file content."
    assert loaded.messages[1]["tool_calls"][0]["function"]["name"] == "read"


def test_session_store_is_workspace_scoped(tmp_path) -> None:
    """不同工作区应落到不同 hash 目录，不能用相同 id 互相恢复。"""
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    left_store = SessionStore(str(left))
    right_store = SessionStore(str(right))

    session = left_store.create()

    assert left_store.workspace_hash != right_store.workspace_hash
    assert workspace_fingerprint(str(left)) == left_store.workspace_hash
    with pytest.raises(Exception, match="Session not found"):
        right_store.load(session.session_id)


def test_latest_session_uses_most_recent_updated_metadata(tmp_path) -> None:
    """latest 应按 updated_at/session_id 选择当前工作区最近 session。"""
    store = SessionStore(str(tmp_path))
    first = store.create()
    second = store.create()
    second.record_prompt("newer")

    latest = store.load("latest")

    assert latest.session_id == second.session_id
    assert first.session_id != second.session_id


def test_append_message_rolls_back_memory_when_persistence_fails(tmp_path, monkeypatch) -> None:
    """写盘失败时 append_message 必须回滚内存，避免内存和 JSONL 分叉。"""
    session = SessionStore(str(tmp_path)).create()

    def fail_append(_record):  # type: ignore[no-untyped-def]
        raise OSError("disk full")

    monkeypatch.setattr(session, "_append_jsonl_record", fail_append)

    with pytest.raises(SessionPersistenceError):
        session.append_message({"role": "user", "content": "hello"})

    assert session.messages == []


def test_session_snapshot_records_compaction_metadata(tmp_path) -> None:
    """replace_messages 应把压缩后上下文和 compaction metadata 一起写入 snapshot。"""
    session = SessionStore(str(tmp_path)).create()

    session.replace_messages(
        [{"role": "system", "content": "summary"}],
        compaction={
            "summary_ref": ".dong/context/compact.md",
            "removed_messages": 4,
            "preserved_messages": 1,
        },
    )

    records = _records(session.persistence_path)
    assert any(record["type"] == "compaction" for record in records)
    assert records[-1]["message"] == {"role": "system", "content": "summary"}


def test_session_persistence_redacts_secrets_and_truncates_large_fields(tmp_path) -> None:
    """session 写盘前应脱敏常见密钥字段并截断超长内容。"""
    session = SessionStore(str(tmp_path)).create()
    large_content = "x" * (MAX_JSON_FIELD_CHARS + 10)

    session.append_message({
        "role": "tool",
        "tool_call_id": "call-1",
        "content": large_content,
        "metadata": {"api_key": "should-not-hit-disk"},
    })

    rendered = session.persistence_path.read_text(encoding="utf-8")
    assert "should-not-hit-disk" not in rendered
    assert "[redacted]" in rendered
    assert "[truncated 10 chars]" in rendered
