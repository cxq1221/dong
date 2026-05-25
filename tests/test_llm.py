"""LLM 封装测试：覆盖 DeepSeek V4 高级参数的请求构造。"""

from __future__ import annotations

from types import SimpleNamespace

from dong import llm


class _FakeCompletions:
    """捕获 chat.completions.create 参数的测试替身。"""

    def __init__(self) -> None:
        self.kwargs: dict | None = None

    def create(self, **kwargs):
        """记录请求参数，并返回最小 ChatCompletion 形状。"""
        self.kwargs = kwargs
        message = SimpleNamespace(content="ok", tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _install_fake_client(monkeypatch):
    """安装假的 OpenAI client，并返回 completions 捕获对象。"""
    completions = _FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(llm, "_client", client)
    return completions


def test_chat_passes_deepseek_thinking_and_reasoning_options(monkeypatch) -> None:
    """DeepSeek V4 thinking 配置应传入 ChatCompletions 请求。"""
    completions = _install_fake_client(monkeypatch)
    monkeypatch.setenv("DONG_THINKING", "disabled")
    monkeypatch.setenv("DONG_REASONING_EFFORT", "max")

    llm.chat([{"role": "user", "content": "hi"}], [], model="deepseek-v4-flash")

    assert completions.kwargs is not None
    assert completions.kwargs["reasoning_effort"] == "max"
    assert completions.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


def test_chat_passes_json_response_format_when_enabled(monkeypatch) -> None:
    """JSON Output opt-in 应传入 response_format=json_object。"""
    completions = _install_fake_client(monkeypatch)
    monkeypatch.setenv("DONG_RESPONSE_FORMAT", "json_object")

    llm.chat([{"role": "user", "content": "json please"}], [], model="deepseek-v4-flash")

    assert completions.kwargs is not None
    assert completions.kwargs["response_format"] == {"type": "json_object"}
