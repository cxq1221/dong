"""fetch 工具测试：覆盖参数校验边界。"""

from dong.tools import execute


def test_fetch_rejects_non_positive_timeout(tmp_path) -> None:
    """timeout 必须为正数，避免 UI 预算和 urlopen 行为不一致。"""
    result = execute(
        "fetch",
        {"url": "https://example.com", "timeout": 0},
        str(tmp_path),
    )

    assert result.success is False
    assert "timeout" in result.error
