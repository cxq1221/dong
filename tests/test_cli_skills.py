"""CLI skill 发现和加载测试：覆盖本地/全局来源、解析和错误提示。"""
import pytest

from dong.cli import (
    build_messages,
    describe_loaded_skills,
    describe_skills,
    list_skills,
    load_skill,
    parse_skill_invocation,
    print_skill_status,
    resolve_skill,
)


def _write(path, content):
    """写入测试文件，并自动创建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_list_skills_merges_local_and_codex_sources(tmp_path, monkeypatch):
    """同名 skill 应合并来源，列表按名称稳定排序。"""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write(tmp_path / ".dong" / "skills" / "python-test.md", "# python-test")
    _write(tmp_path / ".dong" / "skills" / "code-review.md", "# local review")
    _write(codex_home / "skills" / "code-review" / "SKILL.md", "# codex review")
    _write(codex_home / "skills" / "global-only" / "SKILL.md", "# global only")
    _write(codex_home / "skills" / ".system" / "hidden" / "SKILL.md", "# hidden")

    assert list_skills(str(tmp_path)) == ["code-review", "global-only", "python-test"]
    assert describe_skills(str(tmp_path)) == [
        "code-review (local, codex)",
        "global-only (codex)",
        "python-test (local)",
    ]


def test_list_skills_includes_local_skill_directories(tmp_path, monkeypatch):
    """本地目录型 skill 应从 .dong/skills/<name>/SKILL.md 发现。"""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write(
        tmp_path / ".dong" / "skills" / "zoom-out" / "SKILL.md",
        """---
name: zoom-out
description: Broader context
---

# Zoom Out
""",
    )

    assert list_skills(str(tmp_path)) == ["zoom-out"]
    assert describe_skills(str(tmp_path)) == ["zoom-out (local) - Broader context"]

    info, content = load_skill(str(tmp_path), "zoom-out")
    assert info.selected_source == "local"
    assert content.endswith("# Zoom Out")


def test_load_skill_prefers_local_over_codex(tmp_path, monkeypatch):
    """本地 skill 和全局 skill 同名时应优先加载本地版本。"""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write(tmp_path / ".dong" / "skills" / "code-review.md", "# local review")
    _write(codex_home / "skills" / "code-review" / "SKILL.md", "# codex review")

    info, content = load_skill(str(tmp_path), "code-review")

    assert info.selected_source == "local"
    assert info.sources == ("local", "codex")
    assert content == "# local review"


def test_load_skill_falls_back_to_codex_skill_md_raw(tmp_path, monkeypatch):
    """没有本地版本时，应原样读取全局 SKILL.md 内容。"""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    skill_content = """---
name: global-only
description: Test skill
---

# Global Only
"""
    _write(codex_home / "skills" / "global-only" / "SKILL.md", skill_content)

    info, content = load_skill(str(tmp_path), "global-only")

    assert info.selected_source == "codex"
    assert content == skill_content.strip()
    assert content.startswith("---\nname: global-only")


def test_frontmatter_name_is_used_for_discovery_and_alias(tmp_path, monkeypatch):
    """frontmatter name 应作为规范入口名，兼容目录名和展示名不一致。"""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write(
        codex_home / "skills" / "browser-guide" / "SKILL.md",
        """---
name: agent-browser
description: Browser automation guide
allowed-tools: Bash(infsh *)
---

# Browser
""",
    )

    assert list_skills(str(tmp_path)) == ["agent-browser"]
    assert describe_skills(str(tmp_path)) == [
        "agent-browser (codex) - Browser automation guide",
    ]

    info, content = load_skill(str(tmp_path), "agent-browser")
    assert info.name == "agent-browser"
    assert info.description == "Browser automation guide"
    assert info.selected_source == "codex"
    assert "allowed-tools: Bash(infsh *)" in content

    alias_info, _ = load_skill(str(tmp_path), "browser-guide")
    assert alias_info.name == "agent-browser"


def test_frontmatter_description_supports_quoted_values(tmp_path, monkeypatch):
    """frontmatter description 支持简单引号，便于展示来自 SKILL.md 的说明。"""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write(
        tmp_path / ".dong" / "skills" / "review.md",
        """---
name: "code-review"
description: 'Review changed code'
---

# Review
""",
    )

    assert describe_skills(str(tmp_path)) == [
        "code-review (local) - Review changed code",
    ]
    assert resolve_skill(str(tmp_path), "code-review").selected_source == "local"


def test_codex_skill_symlink_targets_are_allowed(tmp_path, monkeypatch):
    """全局 skill 目录允许符号链接目标，兼容外部同步的 skillshare。"""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    target = tmp_path / "skillshare" / "linked-skill"
    _write(target / "SKILL.md", "# Linked Skill")
    skills_dir = codex_home / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "linked-skill").symlink_to(target, target_is_directory=True)

    assert describe_skills(str(tmp_path)) == ["linked-skill (codex)"]
    info, content = load_skill(str(tmp_path), "linked-skill")
    assert info.selected_source == "codex"
    assert content == "# Linked Skill"


def test_build_messages_injects_selected_skill_source(tmp_path, monkeypatch):
    """构建系统消息时应注入已选 skill 的来源和内容。"""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write(codex_home / "skills" / "global-only" / "SKILL.md", "# Global Only")

    messages = build_messages(["global-only"], str(tmp_path))

    assert messages[-1] == {
        "role": "system",
        "content": "--- Skill: global-only (codex) ---\n# Global Only",
    }


def test_build_messages_uses_frontmatter_name_for_alias(tmp_path, monkeypatch):
    """通过路径别名加载时，注入标题应使用 frontmatter 的规范 skill 名。"""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write(
        codex_home / "skills" / "browser-guide" / "SKILL.md",
        "---\nname: agent-browser\n---\n\n# Browser",
    )

    messages = build_messages(["browser-guide"], str(tmp_path))

    assert messages[-1]["content"].startswith("--- Skill: agent-browser (codex) ---")


def test_describe_loaded_skills_reports_current_source(tmp_path, monkeypatch):
    """已加载 skill 状态应展示当前来源，缺失项标记为 missing。"""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write(tmp_path / ".dong" / "skills" / "local-only.md", "# local")
    _write(codex_home / "skills" / "global-only" / "SKILL.md", "# global")

    assert describe_loaded_skills(
        str(tmp_path),
        ["local-only", "global-only", "missing"],
    ) == [
        "local-only (local)",
        "global-only (codex)",
        "missing (missing)",
    ]


def test_parse_skill_invocation_loads_slash_skill_prompt(tmp_path, monkeypatch):
    """`/skill prompt` 快捷语法应解析出 skill 名和后续 prompt。"""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write(codex_home / "skills" / "agent-browser" / "SKILL.md", "# Browser")

    invocation = parse_skill_invocation(str(tmp_path), "/agent-browser count tabs")

    assert invocation is not None
    assert invocation.info.name == "agent-browser"
    assert invocation.info.selected_source == "codex"
    assert invocation.prompt == "count tabs"


def test_parse_skill_invocation_supports_entry_name_alias(tmp_path, monkeypatch):
    """slash 调用应支持目录名别名，并返回 frontmatter 里的规范 skill 名。"""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write(
        codex_home / "skills" / "browser-guide" / "SKILL.md",
        "---\nname: agent-browser\n---\n\n# Browser",
    )

    invocation = parse_skill_invocation(str(tmp_path), "/browser-guide count tabs")

    assert invocation is not None
    assert invocation.info.name == "agent-browser"
    assert invocation.prompt == "count tabs"


def test_parse_skill_invocation_supports_load_only(tmp_path, monkeypatch):
    """只输入 `/skill` 时应解析为加载 skill，但 prompt 为空。"""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write(codex_home / "skills" / "agent-browser" / "SKILL.md", "# Browser")

    invocation = parse_skill_invocation(str(tmp_path), "/agent-browser")

    assert invocation is not None
    assert invocation.info.name == "agent-browser"
    assert invocation.prompt == ""


def test_parse_skill_invocation_ignores_non_slash_input(tmp_path, monkeypatch):
    """非 slash 普通输入不应被解析为 skill 调用。"""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert parse_skill_invocation(str(tmp_path), "agent-browser count tabs") is None


def test_print_skill_status_lists_available_and_loaded(tmp_path, monkeypatch, capsys):
    """状态输出应同时包含可用 skill 和已加载 skill。"""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write(tmp_path / ".dong" / "skills" / "python-test.md", "# local")
    _write(codex_home / "skills" / "agent-browser" / "SKILL.md", "# Browser")

    print_skill_status(str(tmp_path), ["agent-browser"])

    err = capsys.readouterr().err
    assert "Available:" in err
    assert "agent-browser (codex)" in err
    assert "python-test (local)" in err
    assert "Loaded:" in err


def test_missing_skill_error_lists_local_and_codex_sources(tmp_path, monkeypatch):
    """skill 缺失错误应给出当前可用的本地和全局候选。"""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write(tmp_path / ".dong" / "skills" / "python-test.md", "# python-test")
    _write(codex_home / "skills" / "global-only" / "SKILL.md", "# global only")

    with pytest.raises(FileNotFoundError) as exc:
        load_skill(str(tmp_path), "missing")

    message = str(exc.value)
    assert "Skill 'missing' not found." in message
    assert "python-test (local)" in message
    assert "global-only (codex)" in message


def test_invalid_skill_name_is_rejected(tmp_path, monkeypatch):
    """非法 skill 名应被拒绝，防止路径穿越。"""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    with pytest.raises(FileNotFoundError, match="Invalid skill name"):
        resolve_skill(str(tmp_path), "../secret")
