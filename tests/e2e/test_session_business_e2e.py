"""session 业务端到端测试：从用户入口覆盖不同真实使用角度。"""

from __future__ import annotations

import json
import sys

import pytest

from dong import cli
from dong.session import SessionStore
from e2e.helpers import (
    assistant_message,
    message_contents,
    records,
    run_dong,
    tool_call,
)


def test_customer_support_resume_keeps_order_context(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """客服场景：恢复后应带上上一轮订单处理状态。"""
    workdir = tmp_path / "support"

    run_dong(
        monkeypatch,
        workdir,
        ["客户 A 的订单 42 已退款，请记住处理状态"],
        [assistant_message(content="已记录：订单 42 已退款。")],
    )
    seen = run_dong(
        monkeypatch,
        workdir,
        ["--resume", "latest", "继续处理这个客户的下一步"],
        [assistant_message(content="继续处理订单 42。")],
    )

    contents = [
        message.get("content")
        for message in seen[0]
        if isinstance(message, dict)
    ]
    assert contents == [
        "客户 A 的订单 42 已退款，请记住处理状态",
        "已记录：订单 42 已退款。",
        "继续处理这个客户的下一步",
    ]


def test_file_audit_resume_can_use_previous_tool_result(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审计场景：上一轮 read 工具结果应保留到 resume 后的模型请求。"""
    workdir = tmp_path / "audit"
    workdir.mkdir()
    (workdir / "incident.txt").write_text("risk: leaked token\n", encoding="utf-8")

    run_dong(
        monkeypatch,
        workdir,
        ["审计 incident.txt"],
        [
            assistant_message(
                tool_calls=[
                    tool_call("call-1", "read", '{"filepath": "incident.txt"}')
                ],
            ),
            assistant_message(content="发现风险：leaked token。"),
        ],
    )
    seen = run_dong(
        monkeypatch,
        workdir,
        ["--resume", "latest", "基于刚才读取结果总结风险"],
        [assistant_message(content="风险来自 incident.txt。")],
    )

    assert any(
        isinstance(message, dict)
        and message.get("role") == "tool"
        and "leaked token" in message.get("content", "")
        for message in seen[0]
    )


def test_codegen_write_tool_creates_file_and_persists_tool_message(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """代码生成场景：write 工具应真实写文件，并把工具结果写入 session。"""
    workdir = tmp_path / "codegen"

    run_dong(
        monkeypatch,
        workdir,
        ["生成发布说明"],
        [
            assistant_message(
                tool_calls=[
                    tool_call(
                        "call-1",
                        "write",
                        '{"filepath": "release.md", "content": "# Release\\n- shipped"}',
                    )
                ],
            ),
            assistant_message(content="发布说明已生成。"),
        ],
    )

    assert (workdir / "release.md").read_text(encoding="utf-8") == (
        "# Release\n- shipped"
    )
    assert any(
        record.get("type") == "message"
        and record.get("message", {}).get("role") == "tool"
        and "release.md" in record.get("message", {}).get("content", "")
        for record in records(workdir)
    )


def test_skill_assisted_review_injects_skill_and_records_user_prompt(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """评审场景：--skill 应注入 skill 内容，同时 session 记录用户入口。"""
    workdir = tmp_path / "skill-review"
    skill_path = workdir / ".dong" / "skills" / "review.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("# Review\n请按安全审查清单输出。", encoding="utf-8")
    seen_instructions: list[str] = []

    def fake_chat(messages, _tools, instructions="", **_kwargs):  # type: ignore[no-untyped-def]
        seen_instructions.append(instructions)
        assert messages[-1]["content"] == "检查登录模块"
        return assistant_message(content="安全审查完成。")

    monkeypatch.setattr(cli, "chat", fake_chat)
    monkeypatch.setattr(
        sys,
        "argv",
        ["dong", "-d", str(workdir), "--skill", "review", "检查登录模块"],
    )

    cli.main()

    assert "Skill: review" in seen_instructions[0]
    assert "安全审查清单" in seen_instructions[0]
    assert "检查登录模块" in message_contents(workdir)


def test_resume_latest_isolated_between_business_workspaces(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多项目场景：latest 只能恢复当前工作区，不应串到其他项目。"""
    left = tmp_path / "left-project"
    right = tmp_path / "right-project"

    run_dong(
        monkeypatch,
        left,
        ["记录左侧项目客户是 LEFT"],
        [assistant_message(content="LEFT 已记录。")],
    )
    run_dong(
        monkeypatch,
        right,
        ["记录右侧项目客户是 RIGHT"],
        [assistant_message(content="RIGHT 已记录。")],
    )
    seen = run_dong(
        monkeypatch,
        left,
        ["--resume", "latest", "恢复当前项目上下文"],
        [assistant_message(content="恢复 LEFT。")],
    )

    rendered = json.dumps(seen[0], ensure_ascii=False, default=str)
    assert "LEFT" in rendered
    assert "RIGHT" not in rendered


def test_security_audit_does_not_persist_secret_payloads_in_logs_or_session(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """安全场景：读取密钥文件时，日志和 session 都不能落原始 secret。"""
    monkeypatch.delenv("DONG_LOG_PAYLOADS", raising=False)
    workdir = tmp_path / "security"
    workdir.mkdir()
    (workdir / "secret.txt").write_text("api_key=super-secret\n", encoding="utf-8")

    run_dong(
        monkeypatch,
        workdir,
        ["审计 secret.txt"],
        [
            assistant_message(
                tool_calls=[
                    tool_call("call-1", "read", '{"filepath": "secret.txt"}')
                ],
            ),
            assistant_message(content="已检查密钥文件。"),
        ],
    )

    log_text = (workdir / "logs" / "dong.log").read_text(encoding="utf-8")
    session_text = next((workdir / ".dong" / "sessions").glob("*/*.jsonl")).read_text(
        encoding="utf-8"
    )
    assert "super-secret" not in log_text
    assert "super-secret" not in session_text
    assert "[redacted]" in session_text


def test_long_research_resume_records_compaction_metadata(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """长研究场景：恢复长会话时触发压缩，并把 compaction metadata 落盘。"""
    monkeypatch.setenv("DONG_CONTEXT_MAX_TOKENS", "1000")
    workdir = tmp_path / "research"
    workdir.mkdir()
    session = SessionStore(str(workdir)).create(model="deepseek-v4-pro")
    for index in range(5):
        session.append_message({
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"research chunk {index} " + ("x" * 900),
        })

    seen = run_dong(
        monkeypatch,
        workdir,
        ["--resume", "latest", "继续总结研究结论"],
        [assistant_message(content="研究结论已更新。")],
    )

    assert seen[0][0]["role"] == "system"
    assert any(record.get("type") == "compaction" for record in records(workdir))
    assert list((workdir / ".dong" / "context").glob("compact-*.md"))


def test_missing_resume_reference_exits_before_llm_call(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """恢复错误场景：缺失 session id 应直接退出，不发起 LLM 请求。"""
    workdir = tmp_path / "missing-resume"
    workdir.mkdir()

    def fail_chat(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("chat should not be called")

    monkeypatch.setattr(cli, "chat", fail_chat)
    monkeypatch.setattr(
        sys,
        "argv",
        ["dong", "-d", str(workdir), "--resume", "missing-session", "hello"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert "Session not found" in capsys.readouterr().err
