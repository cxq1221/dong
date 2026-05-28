"""session 恢复 Module 测试：覆盖列表、恢复和 Transcript 摘要。"""

from __future__ import annotations

from dong.session import SessionStore
from dong.session_recovery import (
    restore_session,
    session_list_items,
    session_transcript_preview,
)


def test_session_list_items_include_recent_prompt_and_assistant_preview(tmp_path) -> None:
    """session 列表项应集中生成用户问题和 assistant 回复预览。"""
    store = SessionStore(str(tmp_path))
    session = store.create(model="deepseek-v4-pro")
    session.record_prompt("inspect current architecture")
    session.append_message({"role": "assistant", "content": "architecture summary"})

    items = session_list_items(str(tmp_path), session)

    assert len(items) == 1
    assert items[0].session_id == session.session_id
    assert items[0].prompt_preview == "inspect current architecture"
    assert items[0].assistant_preview == "architecture summary"
    assert items[0].current is True
    assert f"--resume {session.session_id}" in items[0].resume_command


def test_restore_session_attaches_working_list_and_returns_preview(tmp_path) -> None:
    """恢复 session 应替换当前 working，并返回 UI 可渲染的恢复摘要。"""
    store = SessionStore(str(tmp_path))
    old = store.create(model="deepseek-v4-pro")
    old.append_message({"role": "user", "content": "old session question"})
    old.append_message({"role": "assistant", "content": "old session answer"})
    working = [{"role": "user", "content": "new session question"}]

    result = restore_session(str(tmp_path), old.session_id, working)

    assert result.session.session_id == old.session_id
    assert result.session.messages is working
    assert working == [
        {"role": "user", "content": "old session question"},
        {"role": "assistant", "content": "old session answer"},
    ]
    assert [line.text for line in result.transcript_preview.lines] == [
        "old session question",
        "old session answer",
    ]


def test_session_transcript_preview_truncates_to_recent_display_lines() -> None:
    """恢复摘要应只保留最近消息，避免恢复长 session 时淹没 TUI。"""
    messages = [
        {"role": "user", "content": f"message {index}"}
        for index in range(20)
    ]

    preview = session_transcript_preview(messages)

    assert len(preview.lines) == 12
    assert preview.lines[0].text == "message 8"
    assert preview.lines[-1].text == "message 19"
