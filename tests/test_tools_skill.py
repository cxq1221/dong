"""skill 工具测试：覆盖模型驱动的 skill 发现和加载入口。"""

from __future__ import annotations

from pathlib import Path

from dong.tools import execute


def _write(path: Path, content: str) -> None:
    """写入测试 skill 文件，并自动创建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_skill_search_returns_matching_metadata_without_body(tmp_path: Path, monkeypatch) -> None:
    """skill_search 只返回候选元数据，不把完整 SKILL.md 正文塞进工具结果。"""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    _write(
        tmp_path / ".dong" / "skills" / "browser" / "SKILL.md",
        (
            "---\n"
            "name: browser\n"
            "description: Inspect browser pages\n"
            "keywords: 浏览器, chrome\n"
            "---\n\n"
            "# Browser\n\n"
            "SECRET FULL BODY SHOULD NOT RETURN\n"
        ),
    )

    result = execute("skill_search", {"query": "浏览器", "limit": 5}, str(tmp_path))

    assert result.success is True
    assert "browser" in result.detail
    assert "Inspect browser pages" in result.detail
    assert "SECRET FULL BODY" not in result.detail


def test_skill_load_validates_skill_without_returning_body(tmp_path: Path, monkeypatch) -> None:
    """skill_load 应校验 skill 存在，但不把 SKILL.md 正文作为工具结果返回。"""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    _write(
        tmp_path / ".dong" / "skills" / "browser" / "SKILL.md",
        "# Browser\n\nSECRET FULL BODY SHOULD NOT RETURN\n",
    )

    result = execute("skill_load", {"skill": "browser"}, str(tmp_path))

    assert result.success is True
    assert "browser" in result.summary
    assert "SECRET FULL BODY" not in result.to_message()["content"]
