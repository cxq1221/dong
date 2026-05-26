"""update_plan 工具测试：覆盖计划状态校验和用户可见摘要。"""

from dong.tools import TOOL_DEFS, execute


def test_update_plan_returns_readable_plan(tmp_path) -> None:
    """update_plan 应接受完整计划，并返回包含状态的摘要。"""
    result = execute(
        "update_plan",
        {
            "explanation": "Need a short implementation path.",
            "plan": [
                {"step": "Inspect tool registry", "status": "completed"},
                {"step": "Add plan tool", "status": "in_progress"},
                {"step": "Run focused tests", "status": "pending"},
            ],
        },
        str(tmp_path),
    )

    assert result.success is True
    assert result.summary == "Updated plan (3 steps)"
    assert "Need a short implementation path." in result.detail
    assert "1. [completed] Inspect tool registry" in result.detail
    assert "2. [in_progress] Add plan tool" in result.detail
    assert "3. [pending] Run focused tests" in result.detail


def test_update_plan_rejects_multiple_in_progress_items(tmp_path) -> None:
    """update_plan 应拒绝多个 in_progress，保持计划状态明确。"""
    result = execute(
        "update_plan",
        {
            "plan": [
                {"step": "First active step", "status": "in_progress"},
                {"step": "Second active step", "status": "in_progress"},
            ],
        },
        str(tmp_path),
    )

    assert result.success is False
    assert "at most one plan item may be in_progress" in result.error


def test_update_plan_is_exposed_in_tool_definitions() -> None:
    """update_plan 应作为普通 Dong 工具暴露给模型。"""
    definitions = {tool["function"]["name"]: tool for tool in TOOL_DEFS}

    assert "update_plan" in definitions
    parameters = definitions["update_plan"]["function"]["parameters"]
    assert "plan" in parameters["properties"]
