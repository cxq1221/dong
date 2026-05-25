"""Tests for skill discovery and loading in dong/cli.py."""
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_list_skills_merges_local_and_codex_sources(tmp_path, monkeypatch):
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


def test_load_skill_prefers_local_over_codex(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write(tmp_path / ".dong" / "skills" / "code-review.md", "# local review")
    _write(codex_home / "skills" / "code-review" / "SKILL.md", "# codex review")

    info, content = load_skill(str(tmp_path), "code-review")

    assert info.selected_source == "local"
    assert info.sources == ("local", "codex")
    assert content == "# local review"


def test_load_skill_falls_back_to_codex_skill_md_raw(tmp_path, monkeypatch):
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


def test_codex_skill_symlink_targets_are_allowed(tmp_path, monkeypatch):
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
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write(codex_home / "skills" / "global-only" / "SKILL.md", "# Global Only")

    messages = build_messages(["global-only"], str(tmp_path))

    assert messages[-1] == {
        "role": "system",
        "content": "--- Skill: global-only (codex) ---\n# Global Only",
    }


def test_describe_loaded_skills_reports_current_source(tmp_path, monkeypatch):
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
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write(codex_home / "skills" / "agent-browser" / "SKILL.md", "# Browser")

    invocation = parse_skill_invocation(str(tmp_path), "/agent-browser count tabs")

    assert invocation is not None
    assert invocation.info.name == "agent-browser"
    assert invocation.info.selected_source == "codex"
    assert invocation.prompt == "count tabs"


def test_parse_skill_invocation_supports_load_only(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write(codex_home / "skills" / "agent-browser" / "SKILL.md", "# Browser")

    invocation = parse_skill_invocation(str(tmp_path), "/agent-browser")

    assert invocation is not None
    assert invocation.info.name == "agent-browser"
    assert invocation.prompt == ""


def test_parse_skill_invocation_ignores_non_slash_input(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert parse_skill_invocation(str(tmp_path), "agent-browser count tabs") is None


def test_print_skill_status_lists_available_and_loaded(tmp_path, monkeypatch, capsys):
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
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    with pytest.raises(FileNotFoundError, match="Invalid skill name"):
        resolve_skill(str(tmp_path), "../secret")
