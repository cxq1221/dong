"""自动 skill 路由测试：按用户意图临时选择最匹配的 skill。"""

from __future__ import annotations

from pathlib import Path

from dong.skill_router import route_skills


def _write(path: Path, content: str) -> None:
    """写入测试 skill 文件，并自动创建父目录。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_route_skills_selects_chinese_keyword_match(tmp_path: Path, monkeypatch) -> None:
    """中文意图命中 skill keywords 时，应自动选择对应 skill。"""

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    _write(
        tmp_path / ".dong" / "skills" / "chrome-cdp" / "SKILL.md",
        (
            "---\n"
            "name: chrome-cdp\n"
            "description: Inspect local Chrome pages\n"
            "keywords: 浏览器, 当前页面, Chrome, CDP\n"
            "---\n\n"
            "# Chrome CDP\n"
        ),
    )

    decision = route_skills("帮我看一下当前浏览器页面", str(tmp_path), loaded_skills=[])

    assert decision.selected == ("chrome-cdp",)
    assert decision.candidates[0].name == "chrome-cdp"
    assert "浏览器" in decision.reason or "当前页面" in decision.reason


def test_route_skills_skips_loaded_skill(tmp_path: Path, monkeypatch) -> None:
    """已常驻加载的 skill 不应被自动重复选择。"""

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    _write(
        tmp_path / ".dong" / "skills" / "chrome-cdp" / "SKILL.md",
        (
            "---\n"
            "name: chrome-cdp\n"
            "description: Inspect local Chrome pages\n"
            "keywords: 浏览器, 当前页面\n"
            "---\n\n"
            "# Chrome CDP\n"
        ),
    )

    decision = route_skills(
        "帮我看一下当前浏览器页面",
        str(tmp_path),
        loaded_skills=["chrome-cdp"],
    )

    assert decision.selected == ()
    assert decision.candidates == ()


def test_route_skills_requires_clear_margin(tmp_path: Path, monkeypatch) -> None:
    """两个 skill 分数接近时保持不选择，避免过度自动化。"""

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    _write(
        tmp_path / ".dong" / "skills" / "review" / "SKILL.md",
        "---\nname: review\ndescription: Review code\nkeywords: 代码\n---\n\n# Review\n",
    )
    _write(
        tmp_path / ".dong" / "skills" / "diagnose" / "SKILL.md",
        "---\nname: diagnose\ndescription: Diagnose code\nkeywords: 代码\n---\n\n# Diagnose\n",
    )

    decision = route_skills("看看这段代码", str(tmp_path), loaded_skills=[])

    assert decision.selected == ()
    assert [candidate.name for candidate in decision.candidates] == ["diagnose", "review"]


def test_route_skills_can_be_disabled_by_env(tmp_path: Path, monkeypatch) -> None:
    """DONG_AUTO_SKILL=0 应关闭自动 skill 路由。"""

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("DONG_AUTO_SKILL", "0")
    _write(
        tmp_path / ".dong" / "skills" / "chrome-cdp" / "SKILL.md",
        "---\nname: chrome-cdp\nkeywords: 浏览器\n---\n\n# Chrome CDP\n",
    )

    decision = route_skills("打开浏览器页面", str(tmp_path), loaded_skills=[])

    assert decision.selected == ()
    assert decision.reason == "disabled"
