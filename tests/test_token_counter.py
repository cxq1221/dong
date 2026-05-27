"""Token 计数 Module 测试：覆盖官方路径和 tiktoken 近似路由。"""

from __future__ import annotations

from types import SimpleNamespace

from dong import llm, token_counter


class _FakeDeepSeekTokenizer:
    """模拟 DeepSeek tokenizer，避免测试依赖真实 tokenizer 资产。"""

    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt):
        """返回固定 token 列表，同时确认调用方启用了 chat template。"""
        assert tokenize is True
        assert add_generation_prompt is True
        assert messages[0] == {"role": "system", "content": "system"}
        return [1, 2, 3]

    def encode(self, text):
        """工具 schema 也应被 tokenizer 单独计入预算。"""
        assert "tool" in text
        return [4, 5]


def test_deepseek_uses_official_tokenizer_route(monkeypatch) -> None:
    """DeepSeek 模型应优先走 transformers tokenizer/chat template 路径。"""
    monkeypatch.setattr(
        token_counter,
        "_load_deepseek_tokenizer",
        lambda model: _FakeDeepSeekTokenizer(),
    )

    tokens = token_counter.count_request_tokens(
        [{"role": "user", "content": "hello"}],
        instructions="system",
        tools=[{"type": "function", "function": {"name": "tool"}}],
        model="deepseek-v4-pro",
    )

    assert tokens == 5


def test_unknown_model_uses_tiktoken_approximation(monkeypatch) -> None:
    """未知模型统一退到 tiktoken，并记录 approximate 日志。"""
    events = []
    monkeypatch.setattr(
        token_counter,
        "log_event",
        lambda logger, level, event, **fields: events.append((event, fields)),
    )

    tokens = token_counter.count_request_tokens(
        [{"role": "user", "content": "hello"}],
        model="mistral-large",
    )

    assert tokens > 0
    assert any(
        event == "token_counter_approximate"
        and fields["model"] == "mistral-large"
        for event, fields in events
    )


def test_unknown_openai_model_marks_default_encoding_as_approximate(monkeypatch) -> None:
    """OpenAI 风格模型名如果 tiktoken 不认识，也要标记为近似。"""
    events = []
    monkeypatch.setattr(
        token_counter,
        "log_event",
        lambda logger, level, event, **fields: events.append((event, fields)),
    )

    tokens = token_counter.count_request_tokens(
        [{"role": "user", "content": "hello"}],
        model="gpt-future-unknown",
    )

    assert tokens > 0
    assert any(
        event == "token_counter_approximate"
        and fields["reason"] == "unknown_tiktoken_model"
        for event, fields in events
    )


def test_claude_uses_anthropic_count_tokens(monkeypatch) -> None:
    """Claude 请求前计数应调用 Anthropic messages.count_tokens。"""
    captured = {}

    class _FakeMessages:
        def count_tokens(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(input_tokens=37)

    monkeypatch.setenv("DONG_API_KEY", "test-key")
    monkeypatch.setattr(
        llm,
        "_get_anthropic_client",
        lambda: SimpleNamespace(messages=_FakeMessages()),
    )

    tokens = token_counter.count_request_tokens(
        [{"role": "user", "content": "hi"}],
        instructions="system",
        model="claude-sonnet-4-20250514",
    )

    assert tokens == 37
    assert captured["model"] == "claude-sonnet-4-20250514"
    assert captured["system"] == "system"


def test_claude_count_tokens_failure_falls_back_to_tiktoken(monkeypatch) -> None:
    """Claude 官方计数失败时应退到 tiktoken，而不是旧 JSON 字节估算。"""
    events = []
    monkeypatch.setattr(
        token_counter,
        "log_event",
        lambda logger, level, event, **fields: events.append((event, fields)),
    )

    class _BrokenMessages:
        def count_tokens(self, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setenv("DONG_API_KEY", "test-key")
    monkeypatch.setattr(
        llm,
        "_get_anthropic_client",
        lambda: SimpleNamespace(messages=_BrokenMessages()),
    )

    tokens = token_counter.count_request_tokens(
        [{"role": "user", "content": "hi"}],
        model="claude-sonnet-4-20250514",
    )

    assert tokens > 0
    assert any(
        event == "token_counter_approximate"
        and fields["reason"].startswith("anthropic_count_tokens_failed")
        for event, fields in events
    )
