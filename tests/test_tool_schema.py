"""工具 schema 测试：覆盖 DeepSeek strict tool calling 开关。"""

from __future__ import annotations

from pydantic import BaseModel

from dong.tool import Tool


class _SampleInput(BaseModel):
    """测试工具入参模型。"""

    value: str


def _sample_tool(_args: _SampleInput, _cwd: str):
    """测试工具函数。"""
    return None


def test_tool_schema_omits_strict_by_default(monkeypatch) -> None:
    """默认不启用 strict，保持现有工具 schema 行为。"""
    monkeypatch.delenv("DONG_TOOL_STRICT", raising=False)
    tool = Tool("sample", "Sample tool", _SampleInput, _sample_tool)

    assert "strict" not in tool.schema["function"]


def test_tool_schema_adds_strict_when_enabled(monkeypatch) -> None:
    """DONG_TOOL_STRICT=1 时，工具 schema 应启用 strict mode。"""
    monkeypatch.setenv("DONG_TOOL_STRICT", "1")
    tool = Tool("sample", "Sample tool", _SampleInput, _sample_tool)

    assert tool.schema["function"]["strict"] is True
