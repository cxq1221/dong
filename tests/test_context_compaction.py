"""上下文压缩 Module 回归测试：覆盖模型预算、请求估算和压缩触发。"""

from __future__ import annotations

from dong import context_compaction as cc


def test_deepseek_v4_uses_model_aware_token_budget(monkeypatch) -> None:
    """DeepSeek V4 默认应按 1M 上下文窗口派生 80% 自动压缩阈值。"""
    monkeypatch.delenv("DONG_CONTEXT_MAX_TOKENS", raising=False)
    monkeypatch.delenv("DONG_CONTEXT_AUTO_COMPACT_PERCENT", raising=False)
    monkeypatch.delenv("DONG_MAX_TOKENS", raising=False)

    budget = cc.build_context_budget("openai/deepseek-v4-flash")

    assert budget.context_window_tokens == 1_000_000
    assert budget.limit == 800_000
    assert budget.max_output_tokens == 8192


def test_manual_token_budget_override_wins(monkeypatch) -> None:
    """显式 DONG_CONTEXT_MAX_TOKENS 应直接控制压缩触发阈值。"""
    monkeypatch.setenv("DONG_CONTEXT_MAX_TOKENS", "500000")

    budget = cc.build_context_budget("deepseek-v4-pro")

    assert budget.limit == 500000
    assert budget.context_window_tokens == 1_000_000


def test_request_estimate_counts_instructions_and_tools() -> None:
    """请求估算必须覆盖 system instructions 和工具 schema，而不仅是消息正文。"""
    messages = [{"role": "user", "content": "hello"}]
    base = cc.estimate_request_tokens(messages, model="gpt-4.1")
    with_context = cc.estimate_request_tokens(
        messages,
        instructions="system" * 100,
        tools=[{"type": "function", "function": {"description": "tool" * 100}}],
        model="gpt-4.1",
    )

    assert with_context > base


def test_old_json_byte_estimator_is_removed() -> None:
    """新项目不再保留 JSON bytes / 4 旧估算入口。"""
    assert not hasattr(cc, "estimate_serialized_tokens")


def test_token_budget_compacts_short_but_large_history(tmp_path, monkeypatch) -> None:
    """短历史也可能因大工具输出超预算，应保留尾部并压缩旧消息。"""
    monkeypatch.setenv("DONG_CONTEXT_MAX_TOKENS", "1000")
    messages = [
        {"role": "user", "content": "最初目标：优化上下文压缩"},
        {"role": "assistant", "content": "旧分析 " + "x" * 3000},
        {"role": "user", "content": "中间请求 " + "y" * 3000},
        {"role": "assistant", "content": "中间回复 " + "z" * 3000},
        {"role": "user", "content": "最新请求：继续执行"},
    ]

    result = cc.compact_context(
        messages,
        max_len=20,
        workdir=str(tmp_path),
        model="deepseek-v4-pro",
        instructions="system",
        tools=[],
        reason="test",
    )

    assert result.compacted
    assert result.removed_messages == 1
    assert result.messages[0]["role"] == "system"
    assert "最初目标" in result.messages[0]["content"]
