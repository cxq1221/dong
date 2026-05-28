"""LLM 封装测试：覆盖 DeepSeek V4 高级参数的请求构造。"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from dong import llm


class _FakeCompletions:
    """捕获 ChatCompletions create 参数的测试替身。"""

    def __init__(self) -> None:
        self.kwargs: dict | None = None
        self.stream_chunks: list | None = None
        self.usage = None

    def create(self, **kwargs):
        """记录请求参数，并返回最小 ChatCompletion 形状。"""
        self.kwargs = kwargs
        if kwargs.get("stream"):
            return iter(self.stream_chunks or [])
        message = SimpleNamespace(content="ok", tool_calls=[])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=self.usage,
        )


class _FakeResponses:
    """捕获 Responses create 参数的测试替身。"""

    def __init__(self) -> None:
        self.kwargs: dict | None = None
        self.usage = None

    def create(self, **kwargs):
        """记录请求参数，并返回最小 Responses 形状。"""
        self.kwargs = kwargs
        return SimpleNamespace(output_text="ok", output=[], usage=self.usage)


class _FakeAnthropicMessages:
    """捕获 Anthropic messages.create 参数的测试替身。"""

    def __init__(self) -> None:
        self.kwargs: dict | None = None
        self.stream_kwargs: dict | None = None
        self.stream_events: list = []
        self.stream_final_message = SimpleNamespace(content=[{"type": "text", "text": "ok"}])
        self.usage = None

    def create(self, **kwargs):
        """记录请求参数，并返回最小 Anthropic Message 形状。"""
        self.kwargs = kwargs
        return SimpleNamespace(content=[{"type": "text", "text": "ok"}], usage=self.usage)

    def stream(self, **kwargs):
        """记录流式请求参数，并返回可迭代的 Anthropic stream 替身。"""
        self.stream_kwargs = kwargs
        return _FakeAnthropicStream(
            events=self.stream_events,
            final_message=self.stream_final_message,
        )


class _FakeAnthropicStream:
    """模拟 Anthropic SDK 的 MessageStreamManager / MessageStream 行为。"""

    def __init__(self, *, events: list, final_message) -> None:
        self.events = events
        self.final_message = final_message

    def __enter__(self):
        """进入 stream 上下文后返回自身，供 Adapter 迭代事件。"""
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False

    def __iter__(self):
        """按测试设置返回流式事件。"""
        return iter(self.events)

    def get_final_message(self):
        """返回 SDK 累积后的最终 Message。"""
        return self.final_message


def _install_fake_client(monkeypatch):
    """安装假的 OpenAI client，并返回 completions 和 responses 捕获对象。"""
    completions = _FakeCompletions()
    responses = _FakeResponses()
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        responses=responses,
    )
    monkeypatch.setattr(llm, "_openai_client", client)
    monkeypatch.setattr(llm, "_resolved_api_mode", None)
    return completions, responses


def _install_fake_anthropic_client(monkeypatch):
    """安装假的 Anthropic client，并返回 messages 捕获对象。"""
    messages = _FakeAnthropicMessages()
    client = SimpleNamespace(messages=messages)
    monkeypatch.setattr(llm, "_anthropic_client", client)
    monkeypatch.setattr(llm, "_resolved_api_mode", None)
    return messages


def _stream_chunk(delta):
    """构造 ChatCompletions 流式 chunk，覆盖文本和工具增量测试。"""
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _anthropic_stream_event(delta):
    """构造 Anthropic content_block_delta 事件，覆盖文本和 thinking 增量。"""
    return SimpleNamespace(type="content_block_delta", delta=delta)


def test_load_env_files_reads_home_dong_env(monkeypatch, tmp_path) -> None:
    """uv tool 安装入口没有项目 .env 时，应能读取 ~/.dong/.env。"""
    home_env = tmp_path / ".dong" / ".env"
    home_env.parent.mkdir()
    home_env.write_text("DONG_API_KEY=home-key\nDONG_LLM_API=anthropic\n", encoding="utf-8")
    monkeypatch.delenv("DONG_API_KEY", raising=False)
    monkeypatch.delenv("DONG_LLM_API", raising=False)

    llm._load_env_files([home_env])

    assert os.environ["DONG_API_KEY"] == "home-key"
    assert os.environ["DONG_LLM_API"] == "anthropic"


def test_load_env_files_keeps_existing_environment(monkeypatch, tmp_path) -> None:
    """shell 显式传入的环境变量优先级应高于 .env 文件。"""
    env_file = tmp_path / ".env"
    env_file.write_text("DONG_API_KEY=file-key\n", encoding="utf-8")
    monkeypatch.setenv("DONG_API_KEY", "shell-key")

    llm._load_env_files([env_file])

    assert os.environ["DONG_API_KEY"] == "shell-key"


def test_llm_timeout_seconds_uses_single_env_with_safe_default(monkeypatch) -> None:
    """LLM SDK 超时只由 DONG_LLM_TIMEOUT 控制，非法值回退到安全默认。"""
    monkeypatch.delenv("DONG_LLM_TIMEOUT", raising=False)
    assert llm._llm_timeout_seconds() == 30.0

    monkeypatch.setenv("DONG_LLM_TIMEOUT", "7.5")
    assert llm._llm_timeout_seconds() == 7.5

    monkeypatch.setenv("DONG_LLM_TIMEOUT", "bad")
    assert llm._llm_timeout_seconds() == 30.0


def test_chat_passes_deepseek_thinking_and_reasoning_options(monkeypatch) -> None:
    """DeepSeek V4 thinking 配置应传入 ChatCompletions 请求。"""
    completions, _responses = _install_fake_client(monkeypatch)
    monkeypatch.setenv("DONG_LLM_API", "chat")
    monkeypatch.setenv("DONG_THINKING", "disabled")
    monkeypatch.setenv("DONG_REASONING_EFFORT", "max")

    llm.chat(
        [{"role": "user", "content": "hi"}],
        [],
        model="deepseek-v4-flash",
        instructions="system",
    )

    assert completions.kwargs is not None
    assert completions.kwargs["messages"][0] == {"role": "system", "content": "system"}
    assert completions.kwargs["reasoning_effort"] == "max"
    assert completions.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


def test_chat_passes_json_response_format_when_enabled(monkeypatch) -> None:
    """JSON Output opt-in 应传入 response_format=json_object。"""
    completions, _responses = _install_fake_client(monkeypatch)
    monkeypatch.setenv("DONG_LLM_API", "chat")
    monkeypatch.setenv("DONG_RESPONSE_FORMAT", "json_object")

    llm.chat([{"role": "user", "content": "json please"}], [], model="deepseek-v4-flash")

    assert completions.kwargs is not None
    assert completions.kwargs["response_format"] == {"type": "json_object"}


def test_chat_attaches_chat_completion_usage(monkeypatch) -> None:
    """ChatCompletions usage 应挂到返回消息，供运行日志记录真实 token。"""
    completions, _responses = _install_fake_client(monkeypatch)
    completions.usage = SimpleNamespace(
        prompt_tokens=11,
        completion_tokens=7,
        total_tokens=18,
    )
    monkeypatch.setenv("DONG_LLM_API", "chat")

    message = llm.chat([{"role": "user", "content": "usage"}], [], model="deepseek-v4-flash")

    assert message.usage.input_tokens == 11
    assert message.usage.output_tokens == 7
    assert message.usage.total_tokens == 18


def test_chat_streams_text_deltas_and_returns_final_message(monkeypatch) -> None:
    """ChatCompletions 流式文本应边到边回调，同时累积成完整 assistant 消息。"""
    completions, _responses = _install_fake_client(monkeypatch)
    completions.stream_chunks = [
        _stream_chunk(SimpleNamespace(content="Hel")),
        _stream_chunk(SimpleNamespace(content="lo", model_extra={"reasoning_content": "r"})),
    ]
    monkeypatch.setenv("DONG_LLM_API", "chat")
    deltas: list[str] = []

    message = llm.chat(
        [{"role": "user", "content": "hi"}],
        [],
        model="deepseek-v4-flash",
        on_text_delta=deltas.append,
    )

    assert completions.kwargs is not None
    assert completions.kwargs["stream"] is True
    assert deltas == ["Hel", "lo"]
    assert message.content == "Hello"
    assert message.tool_calls == []
    assert message.reasoning_content == "r"


def test_chat_completion_stream_raises_task_cancelled(monkeypatch) -> None:
    """ChatCompletions 流式读取时应轮询取消信号并中断当前任务。"""
    completions, _responses = _install_fake_client(monkeypatch)
    completions.stream_chunks = [
        _stream_chunk(SimpleNamespace(content="Hel")),
        _stream_chunk(SimpleNamespace(content="lo")),
    ]
    monkeypatch.setenv("DONG_LLM_API", "chat")
    checks = iter([False, False, True])

    with pytest.raises(llm.TaskCancelled):
        llm.chat(
            [{"role": "user", "content": "hi"}],
            [],
            model="deepseek-v4-flash",
            on_text_delta=lambda _delta: None,
            cancel_requested=lambda: next(checks),
        )


def test_chat_streams_tool_call_deltas_into_tool_calls(monkeypatch) -> None:
    """ChatCompletions 流式工具调用参数片段应拼回 CLI 可执行的 tool_calls。"""
    completions, _responses = _install_fake_client(monkeypatch)
    completions.stream_chunks = [
        _stream_chunk(SimpleNamespace(
            tool_calls=[
                SimpleNamespace(
                    index=0,
                    id="call-1",
                    function=SimpleNamespace(name="read", arguments='{"filepath":'),
                )
            ],
        )),
        _stream_chunk(SimpleNamespace(
            tool_calls=[
                SimpleNamespace(
                    index=0,
                    function=SimpleNamespace(arguments=' "README.md"}'),
                )
            ],
        )),
    ]
    monkeypatch.setenv("DONG_LLM_API", "chat")

    message = llm.chat(
        [{"role": "user", "content": "read"}],
        [],
        model="deepseek-v4-flash",
        on_text_delta=lambda _delta: None,
    )

    assert message.content == ""
    assert message.tool_calls[0].id == "call-1"
    assert message.tool_calls[0].function.name == "read"
    assert message.tool_calls[0].function.arguments == '{"filepath": "README.md"}'


def test_chat_normalizes_streamed_tool_call_history(monkeypatch) -> None:
    """流式构造的 assistant tool_call 下一轮应转成 ChatCompletions dict 历史。"""
    completions, _responses = _install_fake_client(monkeypatch)
    monkeypatch.setenv("DONG_LLM_API", "chat")
    assistant = SimpleNamespace(
        role="assistant",
        content="我先读文件。",
        tool_calls=[
            SimpleNamespace(
                id="call-1",
                function=SimpleNamespace(
                    name="read",
                    arguments='{"filepath": "README.md"}',
                ),
            )
        ],
    )

    llm.chat(
        [
            assistant,
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "[ok] file content",
            },
        ],
        [],
        model="deepseek-v4-flash",
    )

    assert completions.kwargs is not None
    assert completions.kwargs["messages"] == [
        {
            "role": "assistant",
            "content": "我先读文件。",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "read",
                    "arguments": '{"filepath": "README.md"}',
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "[ok] file content",
        },
    ]


def test_chat_preserves_assistant_reasoning_content_for_next_turn(monkeypatch) -> None:
    """DeepSeek thinking 模式要求下一轮 assistant 历史回传 reasoning_content。"""
    completions, _responses = _install_fake_client(monkeypatch)
    monkeypatch.setenv("DONG_LLM_API", "chat")
    assistant = SimpleNamespace(
        role="assistant",
        content="",
        reasoning_content="need to inspect files first",
        tool_calls=[
            SimpleNamespace(
                id="call-1",
                function=SimpleNamespace(
                    name="read",
                    arguments='{"filepath": "DONG.md"}',
                ),
            )
        ],
    )

    llm.chat(
        [
            assistant,
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "[ok] file content",
            },
        ],
        [],
        model="deepseek-v4-flash",
    )

    assert completions.kwargs is not None
    assert completions.kwargs["messages"][0]["reasoning_content"] == (
        "need to inspect files first"
    )


def test_default_mode_uses_anthropic_messages(monkeypatch) -> None:
    """未显式配置 DONG_LLM_API 时，应默认走 Anthropic Messages。"""
    anthropic_messages = _install_fake_anthropic_client(monkeypatch)
    monkeypatch.delenv("DONG_LLM_API", raising=False)

    def fail_openai_client():
        raise AssertionError("OpenAI client should not be used by default")

    monkeypatch.setattr(llm, "_get_openai_client", fail_openai_client)

    llm.chat([{"role": "user", "content": "hi"}], [], instructions="system prompt")

    assert anthropic_messages.kwargs is not None
    assert anthropic_messages.kwargs["model"] == "deepseek-v4-pro"
    assert anthropic_messages.kwargs["system"] == "system prompt"


def test_responses_mode_uses_responses_instructions(monkeypatch) -> None:
    """responses 模式应使用 Responses API，并把系统提示词放到 instructions。"""
    _completions, responses = _install_fake_client(monkeypatch)
    monkeypatch.setenv("DONG_LLM_API", "responses")

    llm.chat(
        [{"role": "user", "content": "hi"}],
        [{
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read file",
                "parameters": {"type": "object"},
                "strict": True,
            },
        }],
        model="gpt-4o",
        instructions="system prompt",
    )

    assert responses.kwargs is not None
    assert responses.kwargs["instructions"] == "system prompt"
    assert responses.kwargs["input"] == [{"role": "user", "content": "hi"}]
    assert responses.kwargs["tools"] == [{
        "type": "function",
        "name": "read",
        "description": "Read file",
        "parameters": {"type": "object"},
        "strict": True,
    }]


def test_responses_mode_attaches_usage(monkeypatch) -> None:
    """Responses API usage 应转成统一 input/output/total 字段。"""
    _completions, responses = _install_fake_client(monkeypatch)
    responses.usage = {
        "input_tokens": 13,
        "output_tokens": 5,
        "total_tokens": 18,
    }
    monkeypatch.setenv("DONG_LLM_API", "responses")

    message = llm.chat([{"role": "user", "content": "usage"}], [], model="gpt-4o")

    assert message.usage.input_tokens == 13
    assert message.usage.output_tokens == 5
    assert message.usage.total_tokens == 18


def test_responses_json_output_uses_text_format(monkeypatch) -> None:
    """Responses API 的 JSON Output 应放到 text.format。"""
    _completions, responses = _install_fake_client(monkeypatch)
    monkeypatch.setenv("DONG_LLM_API", "responses")
    monkeypatch.setenv("DONG_RESPONSE_FORMAT", "json_object")

    llm.chat([{"role": "user", "content": "json"}], [], model="gpt-4o", instructions="system")

    assert responses.kwargs is not None
    assert responses.kwargs["text"] == {"format": {"type": "json_object"}}


def test_anthropic_puts_instructions_in_top_level_system(monkeypatch) -> None:
    """Anthropic Messages API 应把 instructions 放在顶层 system 参数。"""
    anthropic_messages = _install_fake_anthropic_client(monkeypatch)
    monkeypatch.setenv("DONG_LLM_API", "anthropic")

    llm.chat(
        [{"role": "user", "content": "hi"}],
        [],
        model="claude-sonnet-4-5",
        instructions="system prompt",
    )

    assert anthropic_messages.kwargs is not None
    assert anthropic_messages.kwargs["system"] == "system prompt"
    assert anthropic_messages.kwargs["messages"] == [{"role": "user", "content": "hi"}]


def test_anthropic_converts_function_tools(monkeypatch) -> None:
    """OpenAI function schema 应转换为 Anthropic name/input_schema 形状。"""
    anthropic_messages = _install_fake_anthropic_client(monkeypatch)
    monkeypatch.setenv("DONG_LLM_API", "anthropic")

    llm.chat(
        [{"role": "user", "content": "read"}],
        [{
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read file",
                "parameters": {"type": "object"},
                "strict": True,
            },
        }],
        model="deepseek-v4-flash",
    )

    assert anthropic_messages.kwargs is not None
    assert anthropic_messages.kwargs["tools"] == [{
        "name": "read",
        "description": "Read file",
        "input_schema": {"type": "object"},
    }]


def test_anthropic_attaches_usage(monkeypatch) -> None:
    """Anthropic usage 应包含缓存 token，并合并到 total_tokens。"""
    anthropic_messages = _install_fake_anthropic_client(monkeypatch)
    anthropic_messages.usage = SimpleNamespace(
        input_tokens=20,
        output_tokens=9,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=4,
    )
    monkeypatch.setenv("DONG_LLM_API", "anthropic")

    message = llm.chat([{"role": "user", "content": "usage"}], [], model="deepseek-v4-flash")

    assert message.usage.input_tokens == 20
    assert message.usage.output_tokens == 9
    assert message.usage.total_tokens == 36


def test_anthropic_response_tool_use_becomes_tool_calls(monkeypatch) -> None:
    """Anthropic tool_use block 应转换成 CLI 已消费的 tool_calls。"""
    anthropic_messages = _install_fake_anthropic_client(monkeypatch)
    anthropic_messages.create = lambda **kwargs: SimpleNamespace(content=[{
        "type": "tool_use",
        "id": "toolu_1",
        "name": "read",
        "input": {"path": "README.md"},
    }])
    monkeypatch.setenv("DONG_LLM_API", "anthropic")

    message = llm.chat(
        [{"role": "user", "content": "read README"}],
        [],
        model="deepseek-v4-flash",
    )

    assert message.content == ""
    assert message.tool_calls[0].id == "toolu_1"
    assert message.tool_calls[0].function.name == "read"
    assert message.tool_calls[0].function.arguments == '{"path": "README.md"}'


def test_anthropic_streams_text_deltas_and_returns_final_message(monkeypatch) -> None:
    """Anthropic 流式文本应复用通用 delta 回调，并返回最终 Message 形状。"""
    anthropic_messages = _install_fake_anthropic_client(monkeypatch)
    anthropic_messages.stream_events = [
        _anthropic_stream_event(SimpleNamespace(type="text_delta", text="Hel")),
        _anthropic_stream_event(SimpleNamespace(type="text_delta", text="lo")),
    ]
    anthropic_messages.stream_final_message = SimpleNamespace(
        content=[{"type": "text", "text": "Hello"}],
    )
    monkeypatch.setenv("DONG_LLM_API", "anthropic")
    deltas: list[str] = []

    message = llm.chat(
        [{"role": "user", "content": "hi"}],
        [],
        model="claude-sonnet-4-5",
        instructions="system prompt",
        on_text_delta=deltas.append,
    )

    assert anthropic_messages.stream_kwargs is not None
    assert anthropic_messages.stream_kwargs["system"] == "system prompt"
    assert deltas == ["Hel", "lo"]
    assert message.content == "Hello"
    assert message.tool_calls == []


def test_anthropic_stream_raises_task_cancelled(monkeypatch) -> None:
    """Anthropic 流式读取时也应响应同一取消信号。"""
    anthropic_messages = _install_fake_anthropic_client(monkeypatch)
    anthropic_messages.stream_events = [
        _anthropic_stream_event(SimpleNamespace(type="text_delta", text="Hel")),
        _anthropic_stream_event(SimpleNamespace(type="text_delta", text="lo")),
    ]
    monkeypatch.setenv("DONG_LLM_API", "anthropic")
    checks = iter([False, False, True])

    with pytest.raises(llm.TaskCancelled):
        llm.chat(
            [{"role": "user", "content": "hi"}],
            [],
            model="claude-sonnet-4-5",
            on_text_delta=lambda _delta: None,
            cancel_requested=lambda: next(checks),
        )


def test_anthropic_stream_final_message_can_return_tool_calls(monkeypatch) -> None:
    """Anthropic stream 结束后的 final Message 应继续转换为 Dong tool_calls。"""
    anthropic_messages = _install_fake_anthropic_client(monkeypatch)
    anthropic_messages.stream_events = []
    anthropic_messages.stream_final_message = SimpleNamespace(content=[{
        "type": "tool_use",
        "id": "toolu_1",
        "name": "read",
        "input": {"path": "README.md"},
    }])
    monkeypatch.setenv("DONG_LLM_API", "anthropic")

    message = llm.chat(
        [{"role": "user", "content": "read README"}],
        [],
        model="deepseek-v4-flash",
        on_text_delta=lambda _delta: None,
    )

    assert anthropic_messages.stream_kwargs is not None
    assert message.content == ""
    assert message.tool_calls[0].id == "toolu_1"
    assert message.tool_calls[0].function.name == "read"
    assert message.tool_calls[0].function.arguments == '{"path": "README.md"}'


def test_anthropic_preserves_thinking_blocks_for_next_turn(monkeypatch) -> None:
    """Anthropic thinking block 应随 assistant 历史原样回传给下一轮。"""
    anthropic_messages = _install_fake_anthropic_client(monkeypatch)
    monkeypatch.setenv("DONG_LLM_API", "anthropic")
    response = SimpleNamespace(
        content=[
            {"type": "thinking", "thinking": "need file", "signature": "sig-1"},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "read",
                "input": {"path": "README.md"},
            },
        ],
    )
    response_message = llm._anthropic_message(response)

    llm.chat(
        [
            response_message,
            {
                "role": "tool",
                "tool_call_id": "toolu_1",
                "content": "[ok] file content",
            },
        ],
        [],
        model="deepseek-v4-flash",
    )

    assert anthropic_messages.kwargs is not None
    assert anthropic_messages.kwargs["messages"][0] == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "need file", "signature": "sig-1"},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "read",
                "input": {"path": "README.md"},
            },
        ],
    }


def test_anthropic_tool_result_messages_are_user_blocks(monkeypatch) -> None:
    """Dong tool result 消息应转成 Anthropic tool_result user block。"""
    anthropic_messages = _install_fake_anthropic_client(monkeypatch)
    monkeypatch.setenv("DONG_LLM_API", "anthropic")

    llm.chat(
        [
            SimpleNamespace(
                role="assistant",
                content="",
                tool_calls=[
                    SimpleNamespace(
                        id="toolu_1",
                        function=SimpleNamespace(
                            name="read",
                            arguments='{"path": "README.md"}',
                        ),
                    ),
                ],
            ),
            {
                "role": "tool",
                "tool_call_id": "toolu_1",
                "content": "[ok] file content",
            },
        ],
        [],
        model="deepseek-v4-flash",
    )

    assert anthropic_messages.kwargs is not None
    assert anthropic_messages.kwargs["messages"] == [
        {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "toolu_1",
                "name": "read",
                "input": {"path": "README.md"},
            }],
        },
        {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": "[ok] file content",
            }],
        },
    ]


def test_anthropic_mode_does_not_use_openai_client(monkeypatch) -> None:
    """显式 Anthropic 模式不应访问 OpenAI Responses 或 ChatCompletions client。"""
    anthropic_messages = _install_fake_anthropic_client(monkeypatch)
    monkeypatch.setenv("DONG_LLM_API", "anthropic")

    def fail_openai_client():
        raise AssertionError("OpenAI client should not be used")

    monkeypatch.setattr(llm, "_get_openai_client", fail_openai_client)

    llm.chat([{"role": "user", "content": "hi"}], [], model="deepseek-v4-flash")

    assert anthropic_messages.kwargs is not None


def test_anthropic_base_url_selects_anthropic_mode(monkeypatch) -> None:
    """设置 Anthropic base URL 时 auto 模式应直接走 Anthropic adapter。"""
    anthropic_messages = _install_fake_anthropic_client(monkeypatch)
    monkeypatch.setenv("DONG_ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")

    def fail_openai_client():
        raise AssertionError("OpenAI client should not be used")

    monkeypatch.setattr(llm, "_get_openai_client", fail_openai_client)

    llm.chat([{"role": "user", "content": "hi"}], [], model="deepseek-v4-flash")

    assert anthropic_messages.kwargs is not None
