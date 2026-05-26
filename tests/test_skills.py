"""Skill 加载模块的单元测试，覆盖发现、解析和系统提示注入。"""

from __future__ import annotations

from pathlib import Path

from dong.skills import build_skill_messages, list_skills, load_skill


def _write(path: Path, content: str) -> None:
    """写入测试 skill 文件，并自动创建父目录。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_skills_module_discovers_local_directory_skill(tmp_path: Path, monkeypatch) -> None:
    """独立 Skill Module 应能发现本地目录式 SKILL.md。"""

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    _write(
        tmp_path / ".dong" / "skills" / "browser" / "SKILL.md",
        "---\nname: browser\ndescription: Browser helper\n---\n\n# Browser",
    )

    assert list_skills(str(tmp_path)) == ["browser"]
    info, content = load_skill(str(tmp_path), "browser")
    assert info.selected_source == "local"
    assert info.description == "Browser helper"
    assert content.endswith("# Browser")


def test_skills_module_injects_skill_path_metadata(tmp_path: Path, monkeypatch) -> None:
    """Skill 注入应携带 Skill path/dir，保证相对脚本能按 skill 目录解析。"""

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    skill_path = tmp_path / ".dong" / "skills" / "browser" / "SKILL.md"
    _write(skill_path, "# Browser\n\nUse `scripts/cdp.mjs list`.")

    messages = build_skill_messages(["browser"], str(tmp_path))

    assert len(messages) == 1
    content = messages[0]["content"]
    assert f"Skill path: {skill_path}" in content
    assert f"Skill dir: {skill_path.parent}" in content
    assert "relative to Skill dir" in content
