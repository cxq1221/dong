"""dong CLI 入口端到端测试：覆盖单次 prompt 和 REPL 命令路径。"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from contextlib import contextmanager
from io import StringIO

import pytest

from dong import cli
from dong.mcp import mcp_tool_name
from dong.ui import TerminalUI
from e2e.helpers import assistant_message as _assistant_message
from e2e.helpers import tool_call as _tool_call

from mcp_helpers import write_fake_mcp_server, write_mcp_config


def test_system_prompt_is_clear_and_json_mode_aware() -> None:
    """默认系统提示词应来自 dong/default_agent_define.md。"""
    prompt_path = cli.resources.files("dong").joinpath(cli.DEFAULT_AGENT_DEFINE_FILENAME)

    assert cli.SYSTEM_PROMPT == prompt_path.read_text(encoding="utf-8").strip()
    assert "你是在 dong CLI 中运行的编码代理" in cli.SYSTEM_PROMPT


def test_build_agent_prompt_moves_system_messages_to_instructions(tmp_path) -> None:
    """系统类提示词应进入 instructions，普通上下文保留在 input。"""
    (tmp_path / ".dong").mkdir()
    (tmp_path / ".dong" / "DONG.md").write_text("项目规则", encoding="utf-8")

    agent_prompt = cli.build_agent_prompt([], str(tmp_path))

    assert agent_prompt.context_messages == []
    assert cli.SYSTEM_PROMPT in agent_prompt.instructions
    assert "项目规则" in agent_prompt.instructions


def test_build_agent_prompt_ignores_root_agents_md(tmp_path) -> None:
    """根目录 AGENTS.md 是宿主代理规则，dong 不应作为项目规则加载。"""
    (tmp_path / "AGENTS.md").write_text("根目录规则", encoding="utf-8")

    agent_prompt = cli.build_agent_prompt([], str(tmp_path))

    assert "根目录规则" not in agent_prompt.instructions


def test_build_agent_prompt_ignores_dot_dong_agents_md(tmp_path) -> None:
    """.dong/AGENTS.md 是旧入口，dong 不应继续作为项目规则加载。"""
    (tmp_path / ".dong").mkdir()
    (tmp_path / ".dong" / "AGENTS.md").write_text("旧入口规则", encoding="utf-8")

    agent_prompt = cli.build_agent_prompt([], str(tmp_path))

    assert "旧入口规则" not in agent_prompt.instructions


def test_resolve_workdir_uses_repo_root_when_default_starts_in_package_dir(
    tmp_path,
) -> None:
    """未显式传 -d 且从 dong 包目录启动时，应把工具根目录归一到仓库根。"""
    package_dir = tmp_path / "dong"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "dong"\n',
        encoding="utf-8",
    )

    assert cli._resolve_workdir(str(package_dir), explicit=False) == str(tmp_path.resolve())
    assert cli._resolve_workdir(str(package_dir), explicit=True) == str(package_dir.resolve())


@contextmanager
def _fail_after(seconds: float):
    """给可能卡死的同步路径加测试超时保护。"""

    def _raise_timeout(_signum, _frame):
        raise TimeoutError("operation timed out")

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


class RecordingUI(TerminalUI):
    """测试用 UI：记录运行中状态提示，不依赖真实终端 spinner。"""

    def __init__(self) -> None:
        super().__init__(stdout=StringIO(), stderr=StringIO())
        self.working_messages: list[tuple[str, float | None]] = []

    @contextmanager
    def show_working(self, message: str, *, timeout_seconds=None, cancel_hint="Ctrl-C 取消"):
        self.working_messages.append((message, timeout_seconds))
        yield


class TrackingStreamUI(RecordingUI):
    """记录 assistant delta 写入时是否仍有 working 状态占用终端。"""

    def __init__(self) -> None:
        super().__init__()
        self.active_working = 0
        self.delta_working_counts: list[int] = []

    @contextmanager
    def show_working(self, message: str, *, timeout_seconds=None, cancel_hint="Ctrl-C 取消"):
        self.working_messages.append((message, timeout_seconds))
        self.active_working += 1
        try:
            yield
        finally:
            self.active_working -= 1

    @contextmanager
    def stream_assistant_message(self):
        def write_delta(delta: str) -> None:
            self.delta_working_counts.append(self.active_working)
            self.stdout.write(delta)

        yield write_delta


class TrackingReasoningStreamUI(TrackingStreamUI):
    """记录 reasoning delta 是否被 run_loop 接到 UI。"""

    def __init__(self) -> None:
        super().__init__()
        self.reasoning_deltas: list[str] = []

    @contextmanager
    def stream_reasoning_message(self):
        yield self.reasoning_deltas.append


class FinalAssistantPanelUI(RecordingUI):
    """记录最终 assistant 面板渲染次数，用于区分裸流式输出和最终答复。"""

    def __init__(self) -> None:
        super().__init__()
        self.final_messages: list[str] = []

    def show_assistant_message(self, text: str) -> None:
        self.final_messages.append(text)
        super().show_assistant_message(text)


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

    monkeypatch.setattr(
        cli,
        "chat",
        lambda _messages, _tools, instructions="", **_kwargs: next(responses),
    )
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


def test_single_prompt_mode_persists_session_jsonl(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单次 prompt 应创建 session，并把用户和 assistant 消息写入 JSONL。"""
    monkeypatch.setattr(
        cli,
        "chat",
        lambda _messages, _tools, instructions="", **_kwargs: _assistant_message(
            content="Done."
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["dong", "-d", str(tmp_path), "hello"],
    )

    cli.main()

    session_files = list((tmp_path / ".dong" / "sessions").glob("*/*.jsonl"))
    assert len(session_files) == 1
    rendered = session_files[0].read_text(encoding="utf-8")
    assert '"type":"session_meta"' in rendered
    assert '"content":"hello"' in rendered
    assert '"content":"Done."' in rendered


def test_resume_latest_reuses_previous_session_messages(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--resume latest` 应把旧 session 消息带入下一轮模型请求。"""
    from dong.session import SessionStore

    store = SessionStore(str(tmp_path))
    session = store.create(model="deepseek-v4-pro")
    session.append_message({"role": "user", "content": "previous question"})
    session.append_message({"role": "assistant", "content": "previous answer"})
    seen_messages: list[list] = []

    def fake_chat(messages, _tools, instructions="", **_kwargs):  # type: ignore[no-untyped-def]
        seen_messages.append(list(messages))
        return _assistant_message(content="continued")

    monkeypatch.setattr(cli, "chat", fake_chat)
    monkeypatch.setattr(
        sys,
        "argv",
        ["dong", "-d", str(tmp_path), "--resume", "latest", "next question"],
    )

    cli.main()

    contents = [
        message.get("content")
        for message in seen_messages[0]
        if isinstance(message, dict)
    ]
    assert contents == ["previous question", "previous answer", "next question"]


def test_single_prompt_mode_does_not_preselect_matching_skill(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单次 prompt 模式不再按关键词预选 skill，避免干扰模型注意力。"""

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    skill_path = tmp_path / ".dong" / "skills" / "chrome-cdp" / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(
        (
            "---\n"
            "name: chrome-cdp\n"
            "description: Inspect local Chrome pages\n"
            "keywords: 浏览器, 当前页面\n"
            "---\n\n"
            "# Chrome CDP\n"
        ),
        encoding="utf-8",
    )
    seen_instructions: list[str] = []

    def fake_run_loop(base_sys, working, _workdir, *, max_turns, ui, enable_mcp, session=None):  # type: ignore[no-untyped-def]
        seen_instructions.append(base_sys.instructions)

    monkeypatch.setattr(cli, "run_loop", fake_run_loop)
    monkeypatch.setattr(
        sys,
        "argv",
        ["dong", "-d", str(tmp_path), "帮我看一下当前浏览器页面"],
    )

    cli.main()

    assert "Skill: chrome-cdp" not in seen_instructions[0]


def test_skill_load_injects_skill_on_next_model_request(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型调用 skill_load 后，下一次 LLM 请求应注入对应 SKILL.md 内容。"""
    skill_path = tmp_path / ".dong" / "skills" / "chrome-cdp" / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text("# Chrome CDP\n\nUse browser inspection.", encoding="utf-8")
    responses = iter([
        _assistant_message(tool_calls=[
            _tool_call("call-1", "skill_load", '{"skill": "chrome-cdp"}')
        ]),
        _assistant_message(content="ready"),
    ])
    seen_instructions: list[str] = []

    def fake_chat(messages, _tools, instructions="", **_kwargs):  # type: ignore[no-untyped-def]
        seen_instructions.append(instructions)
        return next(responses)

    monkeypatch.setattr(cli, "chat", fake_chat)

    working = [{"role": "user", "content": "inspect browser"}]
    cli.run_loop(cli.build_agent_prompt([], str(tmp_path)), working, str(tmp_path), max_turns=3)

    assert "Skill: chrome-cdp" not in seen_instructions[0]
    assert "Skill: chrome-cdp" in seen_instructions[1]
    tool_messages = [
        item["content"]
        for item in working
        if isinstance(item, dict) and item.get("role") == "tool"
    ]
    assert "Use browser inspection." not in tool_messages[0]


def test_contract_pressure_injected_after_complex_signal(tmp_path, monkeypatch) -> None:
    """复杂信号触发后，下一轮模型请求应看到契约压力摘要。"""
    responses = iter([
        _assistant_message(tool_calls=[
            _tool_call("call-1", "edit", '{"filepath": "a.py", "old": "x", "new": "y"}')
        ]),
        _assistant_message(content="done"),
    ])
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    seen_instructions: list[str] = []

    def fake_chat(_messages, _tools, instructions="", **_kwargs):  # type: ignore[no-untyped-def]
        seen_instructions.append(instructions)
        return next(responses)

    monkeypatch.setattr(cli, "chat", fake_chat)

    cli.run_loop(
        cli.build_agent_prompt([], str(tmp_path)),
        [{"role": "user", "content": "edit file"}],
        str(tmp_path),
        max_turns=3,
    )

    assert "Contract Pressure" not in seen_instructions[0]
    assert "Contract Pressure" in seen_instructions[1]
    assert (tmp_path / ".dong" / "contracts" / "best-practices.md").exists()


def test_contract_artifact_created_after_complex_final_answer(tmp_path, monkeypatch) -> None:
    """复杂任务最终答复后，应生成带签名的契约证据包。"""
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    responses = iter([
        _assistant_message(tool_calls=[
            _tool_call("call-1", "edit", '{"filepath": "a.py", "old": "x", "new": "y"}')
        ]),
        _assistant_message(content="已修改 a.py；未运行测试。"),
    ])

    monkeypatch.setattr(
        cli,
        "chat",
        lambda _messages, _tools, instructions="", **_kwargs: next(responses),
    )

    cli.run_loop(
        cli.build_agent_prompt([], str(tmp_path)),
        [{"role": "user", "content": "edit file"}],
        str(tmp_path),
        max_turns=3,
    )

    artifacts = list((tmp_path / ".dong" / "contracts").glob("session-*.json"))
    assert len(artifacts) == 1
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert payload["signature"]["signature_hash"].startswith("0")
    assert payload["file_changes"]
    assert payload["unverified_items"]


def test_skill_load_caps_model_loaded_skills_at_five(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型每个 user turn 最多临时加载 5 个 skill，超过部分返回失败。"""
    for index in range(6):
        skill_path = tmp_path / ".dong" / "skills" / f"s{index}" / "SKILL.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(f"# Skill {index}\n\nbody {index}", encoding="utf-8")
    responses = iter([
        _assistant_message(tool_calls=[
            _tool_call(f"call-{index}", "skill_load", f'{{"skill": "s{index}"}}')
            for index in range(6)
        ]),
        _assistant_message(content="ready"),
    ])
    seen_instructions: list[str] = []

    def fake_chat(messages, _tools, instructions="", **_kwargs):  # type: ignore[no-untyped-def]
        seen_instructions.append(instructions)
        return next(responses)

    monkeypatch.setattr(cli, "chat", fake_chat)

    working = [{"role": "user", "content": "load skills"}]
    cli.run_loop(cli.build_agent_prompt([], str(tmp_path)), working, str(tmp_path), max_turns=3)

    assert "Skill: s0" in seen_instructions[1]
    assert "Skill: s4" in seen_instructions[1]
    assert "Skill: s5" not in seen_instructions[1]
    tool_messages = [
        item["content"]
        for item in working
        if isinstance(item, dict) and item.get("role") == "tool"
    ]
    assert "Model-loaded skill limit reached" in tool_messages[-1]


def test_run_loop_shows_working_status_for_model_and_tools(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等待模型和执行工具期间，应向 UI 暴露正在工作的状态。"""
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    responses = iter([
        _assistant_message(tool_calls=[
            _tool_call("call-1", "read", '{"filepath": "note.txt"}')
        ]),
        _assistant_message(content="Read complete."),
    ])
    ui = RecordingUI()

    monkeypatch.setattr(
        cli,
        "chat",
        lambda _messages, _tools, instructions="", **_kwargs: next(responses),
    )

    cli.run_loop(
        [{"role": "system", "content": "system"}],
        [{"role": "user", "content": "inspect note"}],
        str(tmp_path),
        max_turns=3,
        ui=ui,
    )

    assert ui.working_messages == [
        ("AI 正在思考...", None),
        ("正在执行工具：read", None),
        ("AI 正在思考...", None),
    ]


def test_run_loop_streams_assistant_text_before_tool_execution(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """带工具调用的 assistant 文本应通过流式 UI 先展示，而不是被最终工具分支吞掉。"""
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    responses = iter([
        _assistant_message(
            content="我先读取文件。",
            tool_calls=[_tool_call("call-1", "read", '{"filepath": "note.txt"}')],
        ),
        _assistant_message(content="Read complete."),
    ])
    ui = RecordingUI()

    def fake_chat(_messages, _tools, instructions="", **kwargs):
        assert instructions
        on_text_delta = kwargs.get("on_text_delta")
        message = next(responses)
        if on_text_delta is not None and message.tool_calls:
            on_text_delta("我先")
            on_text_delta("读取文件。")
        return message

    monkeypatch.setattr(cli, "chat", fake_chat)

    cli.run_loop(
        [{"role": "system", "content": "system"}],
        [{"role": "user", "content": "inspect note"}],
        str(tmp_path),
        max_turns=3,
        ui=ui,
    )

    output = ui.stdout.getvalue()
    assert "assistant" in output
    assert "我先读取文件。" in output
    assert "Read complete." in output


def test_run_loop_streams_reasoning_deltas(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM reasoning delta 应实时转发给支持该接口的 UI。"""
    ui = TrackingReasoningStreamUI()

    def fake_chat(_messages, _tools, instructions="", **kwargs):  # type: ignore[no-untyped-def]
        on_reasoning_delta = kwargs.get("on_reasoning_delta")
        if on_reasoning_delta is not None:
            on_reasoning_delta("先分析")
            on_reasoning_delta("代码。")
        return _assistant_message(content="完成。", reasoning_content="先分析代码。")

    monkeypatch.setattr(cli, "chat", fake_chat)

    cli.run_loop(
        cli.build_agent_prompt([], str(tmp_path)),
        [{"role": "user", "content": "hello"}],
        str(tmp_path),
        ui=ui,
    )

    assert ui.reasoning_deltas == ["先分析", "代码。"]


def test_run_loop_logs_ai_metadata_without_payloads_by_default(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认运行日志只记录长度和状态，不落 AI/工具正文。"""
    monkeypatch.delenv("DONG_LOG_PAYLOADS", raising=False)
    (tmp_path / "note.txt").write_text("api_key=secret-value\n", encoding="utf-8")
    cli.configure_logging(tmp_path, force=True)
    responses = iter([
        _assistant_message(
            content="我先读取 note。",
            reasoning_content="需要先看文件，password=bad-value。",
            tool_calls=[_tool_call("call-1", "read", '{"filepath": "note.txt"}')],
        ),
        _assistant_message(
            content="Read complete token=final-secret.",
            reasoning_content="已经拿到文件内容。",
        ),
    ])

    monkeypatch.setattr(
        cli,
        "chat",
        lambda _messages, _tools, instructions="", **_kwargs: next(responses),
    )

    cli.run_loop(
        cli.build_agent_prompt([], str(tmp_path)),
        [{"role": "user", "content": "inspect note"}],
        str(tmp_path),
        max_turns=3,
    )

    for handler in cli.logging.getLogger("dong").handlers:
        handler.flush()
    rendered = (tmp_path / "logs" / "dong.log").read_text(encoding="utf-8")
    assert "event=ai_message_received" in rendered
    assert '"content_chars":' in rendered
    assert '"reasoning_chars":' in rendered
    assert "需要先看文件" not in rendered
    assert "Read complete" not in rendered
    assert "event=ai_tool_call_requested" in rendered
    assert '"arguments_chars":' in rendered
    assert "arguments_preview" not in rendered
    assert "event=ai_tool_result_received" in rendered
    assert "secret-value" not in rendered
    assert "bad-value" not in rendered
    assert "final-secret" not in rendered


def test_run_loop_payload_logging_uses_redacted_previews(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式开启 payload 日志时，只记录脱敏截断预览。"""
    monkeypatch.setenv("DONG_LOG_PAYLOADS", "1")
    cli.configure_logging(tmp_path, force=True)
    responses = iter([
        _assistant_message(
            content="准备执行。",
            reasoning_content="authorization: Bearer rawtoken",
            tool_calls=[
                _tool_call(
                    "call-1",
                    "read",
                    '{"filepath": "note.txt", "api_key": "raw-secret"}',
                )
            ],
        ),
        _assistant_message(content="Done token=final-secret."),
    ])

    monkeypatch.setattr(
        cli,
        "chat",
        lambda _messages, _tools, instructions="", **_kwargs: next(responses),
    )
    monkeypatch.setattr(
        cli,
        "_execute_registered_tool",
        lambda _name, _args_raw, _workdir, _mcp_manager: cli.ToolResult(
            success=True,
            summary="ok",
            detail="authorization: Bearer tooltoken",
        ),
    )

    cli.run_loop(
        cli.build_agent_prompt([], str(tmp_path)),
        [{"role": "user", "content": "inspect note"}],
        str(tmp_path),
        max_turns=3,
    )

    for handler in cli.logging.getLogger("dong").handlers:
        handler.flush()
    rendered = (tmp_path / "logs" / "dong.log").read_text(encoding="utf-8")
    assert "content_preview" in rendered
    assert "reasoning_preview" in rendered
    assert "arguments_preview" in rendered
    assert "detail_preview" in rendered
    assert "raw-secret" not in rendered
    assert "rawtoken" not in rendered
    assert "tooltoken" not in rendered
    assert "final-secret" not in rendered
    assert "[redacted]" in rendered


def test_run_loop_stops_working_status_before_streaming_text(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首个文本 delta 到达时，应先清理 working 状态，避免 spinner 覆盖正文。"""
    responses = iter([
        _assistant_message(content="hello"),
    ])
    ui = TrackingStreamUI()

    def fake_chat(_messages, _tools, instructions="", **kwargs):
        assert instructions
        on_text_delta = kwargs.get("on_text_delta")
        if on_text_delta is not None:
            on_text_delta("hello")
        return next(responses)

    monkeypatch.setattr(cli, "chat", fake_chat)

    cli.run_loop(
        [{"role": "system", "content": "system"}],
        [{"role": "user", "content": "say hi"}],
        str(tmp_path),
        max_turns=1,
        ui=ui,
    )

    assert ui.delta_working_counts == [0]


def test_run_loop_renders_final_panel_after_streamed_final_answer(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最终答复即使已流式输出，也要补 Markdown 面板，避免总结被状态行干扰。"""
    responses = iter([
        _assistant_message(content="最终总结。"),
    ])
    ui = FinalAssistantPanelUI()

    def fake_chat(_messages, _tools, instructions="", **kwargs):
        assert instructions
        on_text_delta = kwargs.get("on_text_delta")
        if on_text_delta is not None:
            on_text_delta("最终")
            on_text_delta("总结。")
        return next(responses)

    monkeypatch.setattr(cli, "chat", fake_chat)

    cli.run_loop(
        [{"role": "system", "content": "system"}],
        [{"role": "user", "content": "summarize"}],
        str(tmp_path),
        max_turns=1,
        ui=ui,
    )

    assert ui.final_messages == ["最终总结。"]
    assert "最终总结。" in ui.stdout.getvalue()


def test_run_loop_passes_known_tool_timeouts_to_working_status(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bash/fetch 等已知慢工具应向 UI 暴露超时预算，便于显示超时阶段。"""
    responses = iter([
        _assistant_message(tool_calls=[
            _tool_call("call-1", "bash", '{"command": "sleep 1"}'),
            _tool_call("call-2", "fetch", '{"url": "https://example.com", "timeout": 7}'),
        ]),
        _assistant_message(content="Done."),
    ])
    ui = RecordingUI()

    monkeypatch.setattr(
        cli,
        "chat",
        lambda _messages, _tools, instructions="", **_kwargs: next(responses),
    )
    monkeypatch.setattr(
        cli,
        "_execute_registered_tool",
        lambda _name, _args_raw, _workdir, _mcp_manager: cli.ToolResult(success=True),
    )

    cli.run_loop(
        [{"role": "system", "content": "system"}],
        [{"role": "user", "content": "run slow tools"}],
        str(tmp_path),
        max_turns=3,
        ui=ui,
    )

    assert ("正在执行工具：bash", 30.0) in ui.working_messages
    assert ("正在执行工具：fetch", 7.0) in ui.working_messages


def test_run_loop_ctrl_c_during_tool_returns_to_caller(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工具执行期间 Ctrl-C 应中断当前任务并清理回 REPL，而不是冒泡崩溃。"""
    responses = iter([
        _assistant_message(tool_calls=[
            _tool_call("call-1", "bash", '{"command": "sleep 30"}')
        ]),
    ])
    ui = RecordingUI()

    monkeypatch.setattr(
        cli,
        "chat",
        lambda _messages, _tools, instructions="", **_kwargs: next(responses),
    )

    def raise_keyboard_interrupt(_name, _args_raw, _workdir, _mcp_manager):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_execute_registered_tool", raise_keyboard_interrupt)

    cli.run_loop(
        [{"role": "system", "content": "system"}],
        [{"role": "user", "content": "run slow tool"}],
        str(tmp_path),
        max_turns=1,
        ui=ui,
    )

    assert ("正在执行工具：bash", 30.0) in ui.working_messages
    assert "已中断当前任务" in ui.stderr.getvalue()


def test_run_loop_invalid_bash_args_return_tool_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bash 参数非法时应写回工具失败结果，而不是在危险命令预解析处崩溃。"""
    responses = iter([
        _assistant_message(tool_calls=[
            _tool_call("call-1", "bash", '{"cmd": "rm -rf tmp"}')
        ]),
        _assistant_message(content="Handled invalid args."),
    ])
    seen_messages: list[list] = []
    ui = RecordingUI()

    def fake_chat(messages, _tools, instructions="", **_kwargs):
        assert instructions
        seen_messages.append(list(messages))
        return next(responses)

    monkeypatch.setattr(cli, "chat", fake_chat)

    cli.run_loop(
        [{"role": "system", "content": "system"}],
        [{"role": "user", "content": "run bad bash"}],
        str(tmp_path),
        max_turns=2,
        ui=ui,
    )

    assert "Invalid input for bash" in ui.stderr.getvalue()
    assert any(
        isinstance(message, dict)
        and message.get("role") == "tool"
        and "Invalid input for bash" in message.get("content", "")
        for message in seen_messages[1]
    )


def test_run_loop_llm_error_shows_error_panel(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM 普通异常应展示 UI 错误并返回，而不是把 traceback 冒泡给用户。"""
    ui = RecordingUI()

    def raise_llm_error(_messages, _tools, instructions="", **_kwargs):
        raise RuntimeError("provider rejected request")

    monkeypatch.setattr(cli, "chat", raise_llm_error)

    cli.run_loop(
        [{"role": "system", "content": "system"}],
        [{"role": "user", "content": "answer"}],
        str(tmp_path),
        max_turns=1,
        ui=ui,
    )

    rendered = ui.stderr.getvalue()
    assert "AI 请求失败" in rendered
    assert "provider rejected request" in rendered


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

    def fake_chat(messages, _tools, instructions="", **_kwargs):
        assert instructions
        assert all(
            not (isinstance(message, dict) and message.get("role") == "system")
            for message in messages
        )
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

    monkeypatch.setattr(
        cli,
        "chat",
        lambda _messages, _tools, instructions="", **_kwargs: next(responses),
    )

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


def test_trim_context_compacts_old_messages_to_file(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上下文过大时应生成系统摘要，并把完整摘要写入项目文件。"""
    monkeypatch.setenv("DONG_CONTEXT_MAX_TOKENS", "1000")
    messages = [
        {"role": "user", "content": "请记住最初目标：优化上下文策略"},
        {"role": "assistant", "content": "我会先分析现状"},
        {"role": "user", "content": "旧消息 " + "x" * 1200},
        {"role": "assistant", "content": "旧回复 " + "y" * 1200},
        {"role": "user", "content": "最新请求：继续执行"},
        {"role": "assistant", "content": "最新回复"},
    ]

    trimmed = cli.trim_context(messages, max_len=4, workdir=str(tmp_path))

    assert trimmed[0]["role"] == "system"
    assert "Compacted conversation context" in trimmed[0]["content"]
    assert "最新请求：继续执行" in str(trimmed)
    summaries = list((tmp_path / ".dong" / "context").glob("compact-*.md"))
    assert len(summaries) == 1
    assert "优化上下文策略" in summaries[0].read_text(encoding="utf-8")


def test_trim_context_carries_user_goal_across_nested_summaries(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """二次压缩旧摘要时，当前用户目标不能被工具尾部时间线挤掉。"""
    monkeypatch.setenv("DONG_CONTEXT_MAX_TOKENS", "1000")
    prior_summary = "\n".join([
        cli.CONTEXT_SUMMARY_PREFIX,
        "说明：以下内容由 dong 在本地根据旧消息自动压缩生成，不依赖数据库或额外 LLM 调用。",
        "- 很长的旧工具轨迹：" + "x" * 500,
        "- 压缩消息数：4",
        "- 角色分布：assistant=1, tool=2, user=1",
        "- 最近用户请求：",
        "  - user: 挖掘项目优化点",
        "- 压缩前尾部时间线：",
        "  - user: 挖掘项目优化点",
    ])
    messages = [
        {"role": "system", "content": prior_summary},
        _assistant_message(tool_calls=[
            _tool_call("call-1", "read", '{"filepath":"dong/cli.py"}')
        ]),
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "cli.py " + "x" * 2000,
        },
        _assistant_message(tool_calls=[
            _tool_call("call-2", "bash", '{"command":"ls dong/cli.py"}')
        ]),
        {"role": "tool", "tool_call_id": "call-2", "content": "dong/cli.py"},
        _assistant_message(content="已成功读取 cli.py"),
    ]

    trimmed = cli.trim_context(messages, max_len=4, workdir=str(tmp_path))

    assert trimmed[0]["role"] == "system"
    assert "挖掘项目优化点" in trimmed[0]["content"]
    summaries = sorted((tmp_path / ".dong" / "context").glob("compact-*.md"))
    assert "挖掘项目优化点" in summaries[-1].read_text(encoding="utf-8")


def test_trim_context_keeps_tool_call_result_pair(tmp_path) -> None:
    """裁剪边界落在 tool result 上时，应回退保留对应 assistant tool_call。"""
    messages = [
        {"role": "user", "content": "先读文件"},
        _assistant_message(tool_calls=[
            _tool_call("call-1", "read", '{"filepath":"a.txt"}')
        ]),
        {"role": "tool", "tool_call_id": "call-1", "content": "[✓] read a.txt"},
        {"role": "user", "content": "继续"},
        {"role": "assistant", "content": "完成"},
    ]

    trimmed = cli.trim_context(messages, max_len=3, workdir=str(tmp_path))

    roles = [getattr(message, "role", None) or message.get("role") for message in trimmed]
    assert roles == ["system", "assistant", "tool", "user", "assistant"]
    assert getattr(trimmed[1].tool_calls[0], "id") == "call-1"
    assert trimmed[2]["tool_call_id"] == "call-1"


def test_trim_context_does_not_loop_inside_multi_tool_result_group(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连续多个 tool result 超预算时，裁剪边界必须继续前进而不是卡住。"""
    monkeypatch.setenv("DONG_CONTEXT_MAX_TOKENS", "1000")
    messages = [
        {"role": "user", "content": "分析这个项目"},
        _assistant_message(tool_calls=[
            _tool_call("call-1", "bash", '{"command":"ls -la"}'),
            _tool_call("call-2", "bash", '{"command":"find . -maxdepth 2 -type f"}'),
        ]),
        {"role": "tool", "tool_call_id": "call-1", "content": "small output"},
        {"role": "tool", "tool_call_id": "call-2", "content": "small output"},
        _assistant_message(tool_calls=[
            _tool_call("call-3", "read", '{"filepath":"README.md"}'),
            _tool_call("call-4", "read", '{"filepath":"pyproject.toml"}'),
            _tool_call("call-5", "read", '{"filepath":"dong/__init__.py"}'),
            _tool_call("call-6", "read", '{"filepath":"dong/cli.py"}'),
        ]),
        {"role": "tool", "tool_call_id": "call-3", "content": "README " + "x" * 1200},
        {"role": "tool", "tool_call_id": "call-4", "content": "pyproject"},
        {"role": "tool", "tool_call_id": "call-5", "content": "__init__"},
        {"role": "tool", "tool_call_id": "call-6", "content": "cli " + "y" * 5000},
    ]

    with _fail_after(0.5):
        trimmed = cli.trim_context(messages, max_len=14, workdir=str(tmp_path))

    assert trimmed[0]["role"] == "system"
    assert len(trimmed) < len(messages)


def test_run_loop_injects_and_executes_mcp_tool(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_loop 应把 MCP tool 注入模型请求，并执行模型返回的 MCP tool call。"""

    server_script = tmp_path / "fake_mcp.py"
    write_fake_mcp_server(server_script)
    write_mcp_config(tmp_path, server_script)
    tool_name = mcp_tool_name("demo", "echo")
    responses = iter([
        _assistant_message(tool_calls=[
            _tool_call("call-1", tool_name, '{"value": "from model"}')
        ]),
        _assistant_message(content="MCP complete."),
    ])
    seen_tools: list[list] = []
    seen_messages: list[list] = []

    def fake_chat(messages, tools, instructions="", **_kwargs):
        assert instructions
        seen_messages.append(list(messages))
        seen_tools.append(list(tools))
        return next(responses)

    monkeypatch.setattr(cli, "chat", fake_chat)

    cli.run_loop(
        [{"role": "system", "content": "system"}],
        [{"role": "user", "content": "use mcp"}],
        str(tmp_path),
        max_turns=3,
        enable_mcp=True,
    )

    assert any(tool["function"]["name"] == tool_name for tool in seen_tools[0])
    assert any(
        message.get("role") == "tool" and "echo: from model" in message.get("content", "")
        for message in seen_messages[1]
        if isinstance(message, dict)
    )


def test_run_loop_does_not_start_mcp_without_opt_in(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未显式启用 MCP 时，run_loop 不应执行项目 MCP 配置命令。"""

    server_script = tmp_path / "fake_mcp.py"
    write_fake_mcp_server(server_script)
    write_mcp_config(tmp_path, server_script)
    seen_tools: list[list] = []

    def fake_chat(_messages, tools, instructions="", **_kwargs):
        assert instructions
        seen_tools.append(list(tools))
        return _assistant_message(content="No MCP.")

    monkeypatch.setattr(cli, "chat", fake_chat)

    cli.run_loop(
        [{"role": "system", "content": "system"}],
        [{"role": "user", "content": "do not use mcp"}],
        str(tmp_path),
        max_turns=1,
    )

    assert all(not tool["function"]["name"].startswith("mcp__") for tool in seen_tools[0])


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
