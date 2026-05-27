"""CLI 运营与多工具链端到端测试：覆盖子命令、安全边界和工具组合。"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dong import cli
from e2e.helpers import assistant_message, records, run_dong, tool_call
from mcp_helpers import write_fake_mcp_server, write_mcp_config


def _tool_messages(workdir) -> list[str]:
    """读取 session 里的工具消息正文，便于验证完整工具链结果。"""
    return [
        record.get("message", {}).get("content", "")
        for record in records(workdir)
        if record.get("type") == "message"
        and record.get("message", {}).get("role") == "tool"
    ]


def test_logs_subcommand_filters_run_loop_events_from_cli_entry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """运维场景：一次任务完成后，`dong logs` 应能按事件过滤 JSON 日志。"""
    workdir = tmp_path / "ops-logs"
    run_dong(
        monkeypatch,
        workdir,
        ["生成一条日志"],
        [assistant_message(content="日志任务完成。")],
    )
    capsys.readouterr()

    monkeypatch.setattr(
        sys,
        "argv",
        ["dong", "logs", "-d", str(workdir), "--event", "run_loop_finished", "--json"],
    )

    cli.main()

    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    events = [line.get("event") for line in lines if line.get("type") == "log"]
    assert events == ["run_loop_finished"]
    assert lines[0]["type"] == "meta"


def test_mcp_list_tools_subcommand_connects_configured_server(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """集成场景：`dong mcp list --tools` 应启动 server 并展示发现的工具。"""
    server_script = tmp_path / "fake_mcp_server.py"
    write_fake_mcp_server(server_script)
    write_mcp_config(tmp_path, server_script)
    monkeypatch.setattr(
        sys,
        "argv",
        ["dong", "mcp", "list", "-d", str(tmp_path), "--tools"],
    )

    cli.main()

    output = capsys.readouterr().out
    assert "demo\tconnected" in output
    assert "demo__echo" in output


def test_hotfix_edit_then_grep_chain_updates_file_and_context(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """热修场景：模型先 edit 再 grep，应真实改文件并把核验结果回传上下文。"""
    workdir = tmp_path / "hotfix"
    workdir.mkdir()
    (workdir / "service.py").write_text(
        "def status():\n    return 'bug'\n",
        encoding="utf-8",
    )

    seen = run_dong(
        monkeypatch,
        workdir,
        ["修复 service.py 里的状态返回"],
        [
            assistant_message(
                tool_calls=[
                    tool_call(
                        "call-edit",
                        "edit",
                        (
                            '{"filepath": "service.py", '
                            '"old_string": "return \'bug\'", '
                            '"new_string": "return \'fixed\'"}'
                        ),
                    )
                ],
            ),
            assistant_message(
                tool_calls=[
                    tool_call(
                        "call-grep",
                        "grep",
                        '{"pattern": "fixed", "path": "service.py", "head_limit": 5}',
                    )
                ],
            ),
            assistant_message(content="修复并核验完成。"),
        ],
    )

    assert "return 'fixed'" in (workdir / "service.py").read_text(encoding="utf-8")
    assert any("service.py" in message and "fixed" in message for message in _tool_messages(workdir))
    assert len(seen) == 3


def test_skill_search_then_skill_load_injects_only_after_model_choice(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """技能选择场景：先搜索再加载，skill 正文只应在 load 后进入下一次请求。"""
    workdir = tmp_path / "skill-chain"
    skill_path = workdir / ".dong" / "skills" / "api-review" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        (
            "---\n"
            "name: api-review\n"
            "description: Review API compatibility\n"
            "keywords: API, 兼容性, 审查\n"
            "---\n\n"
            "# API Review\n请检查 API 兼容性。\n"
        ),
        encoding="utf-8",
    )
    responses = iter([
        assistant_message(
            tool_calls=[
                tool_call(
                    "call-search",
                    "skill_search",
                    '{"query": "API 兼容性审查", "limit": 3}',
                )
            ],
        ),
        assistant_message(
            tool_calls=[
                tool_call("call-load", "skill_load", '{"skill": "api-review"}')
            ],
        ),
        assistant_message(content="已按 API 审查 skill 工作。"),
    ])
    seen_instructions: list[str] = []

    def fake_chat(messages, _tools, instructions="", **_kwargs):  # type: ignore[no-untyped-def]
        seen_instructions.append(instructions)
        return next(responses)

    monkeypatch.setattr(cli, "chat", fake_chat)
    monkeypatch.setattr(sys, "argv", ["dong", "-d", str(workdir), "检查 API 兼容性"])

    cli.main()

    assert "请检查 API 兼容性" not in seen_instructions[0]
    assert "请检查 API 兼容性" not in seen_instructions[1]
    assert "请检查 API 兼容性" in seen_instructions[2]
    tool_text = "\n".join(_tool_messages(workdir))
    assert "Found " in tool_text
    assert "- api-review:" in tool_text
    assert "Skill queued for this turn: api-review" in tool_text


def test_security_write_path_traversal_is_denied_inside_agent_loop(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """安全场景：模型尝试写出工作区时，应被工具层拒绝且不产生逃逸文件。"""
    workdir = tmp_path / "security-boundary"

    run_dong(
        monkeypatch,
        workdir,
        ["尝试写一个外部文件"],
        [
            assistant_message(
                tool_calls=[
                    tool_call(
                        "call-write",
                        "write",
                        '{"filepath": "../escape.txt", "content": "should not exist"}',
                    )
                ],
            ),
            assistant_message(content="外部写入已被拒绝。"),
        ],
    )

    assert not (tmp_path / "escape.txt").exists()
    assert any("Path traversal denied" in message for message in _tool_messages(workdir))


def test_fetch_local_http_content_flows_back_to_followup_model_request(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """资料抓取场景：fetch 工具读取本地 HTTP 后，下一轮模型应看到页面正文。"""

    class Handler(BaseHTTPRequestHandler):
        """测试 HTTP handler：返回稳定文本，关闭默认访问日志。"""

        def do_GET(self) -> None:  # noqa: N802
            body = "release window: Friday"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, _format: str, *_args) -> None:  # type: ignore[no-untyped-def]
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/release.txt"
    try:
        seen = run_dong(
            monkeypatch,
            tmp_path / "fetch-flow",
            ["抓取发布窗口"],
            [
                assistant_message(
                    tool_calls=[
                        tool_call(
                            "call-fetch",
                            "fetch",
                            json.dumps({"url": url, "timeout": 2}, ensure_ascii=False),
                        )
                    ],
                ),
                assistant_message(content="发布窗口是 Friday。"),
            ],
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    rendered = json.dumps(seen[1], ensure_ascii=False, default=str)
    assert "release window: Friday" in rendered


def test_planning_tool_result_is_persisted_before_final_answer(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """项目管理场景：update_plan 结果应作为工具消息进入 session，再接最终答复。"""
    workdir = tmp_path / "planning"
    plan_payload = {
        "explanation": "按风险推进",
        "plan": [
            {"step": "审查输入", "status": "completed"},
            {"step": "执行修复", "status": "in_progress"},
        ],
    }

    run_dong(
        monkeypatch,
        workdir,
        ["制定修复计划"],
        [
            assistant_message(
                tool_calls=[
                    tool_call(
                        "call-plan",
                        "update_plan",
                        json.dumps(plan_payload, ensure_ascii=False),
                    )
                ],
            ),
            assistant_message(content="计划已同步。"),
        ],
    )

    tool_text = "\n".join(_tool_messages(workdir))
    assert "Updated plan (2 steps)" in tool_text
    assert "1. [completed] 审查输入" in tool_text
    assert "2. [in_progress] 执行修复" in tool_text
