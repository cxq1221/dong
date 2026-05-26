"""Skill 加载模块：集中负责 skill 的发现、解析、选择和系统提示注入。"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass

from dong.logging_config import get_logger, log_event

LOGGER = get_logger(__name__)

SKILLS_RELPATH = ".dong/skills"
CODEX_SKILLS_RELPATH = "skills"


@dataclass(frozen=True)
class SkillInfo:
    """一个 skill 的发现结果和最终选中的来源。"""

    name: str
    description: str | None
    sources: tuple[str, ...]
    selected_source: str
    selected_path: str


@dataclass(frozen=True)
class SkillSource:
    """单个 skill 文件解析出的来源和展示元数据。"""

    name: str
    entry_name: str
    description: str | None
    keywords: tuple[str, ...]
    source: str
    path: str


@dataclass(frozen=True)
class SkillCatalogEntry:
    """自动路由使用的 skill 索引项，只保留稳定且可解释的元数据。"""

    name: str
    entry_name: str
    keywords: tuple[str, ...]
    search_text: str


@dataclass(frozen=True)
class SkillInvocation:
    """slash skill 调用解析后的结果，包含 skill 元信息和用户 prompt。"""

    info: SkillInfo
    prompt: str


def _skills_dir(workdir: str) -> str:
    """返回项目本地 skill 目录。"""

    return os.path.join(workdir, SKILLS_RELPATH)


def _codex_skills_dir() -> str:
    """返回全局 Codex skill 目录，允许 CODEX_HOME 覆盖默认位置。"""

    codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.join(os.path.abspath(codex_home), CODEX_SKILLS_RELPATH)


def _validate_skill_name(name: str) -> None:
    """限制 skill 名只能是单个路径段，防止通过名称做路径穿越。"""

    if not _is_valid_skill_name(name):
        raise FileNotFoundError(f"Invalid skill name: {name!r}")


def _is_valid_skill_name(name: str | None) -> bool:
    """判断 skill 名是否能安全用作命令入口和路径段。"""

    return bool(name) and name == os.path.basename(name) and name not in (".", "..")


def _validate_path_under(root: str, *parts: str, follow_symlinks: bool = True) -> str:
    """把路径解析到 root 内部；可选择是否跟随符号链接。"""

    resolve = os.path.realpath if follow_symlinks else os.path.abspath
    root_path = resolve(root)
    path = resolve(os.path.join(root_path, *parts))
    if path != root_path and not path.startswith(root_path + os.sep):
        raise PermissionError(f"Path traversal denied: {os.path.join(*parts)} → {path}")
    return path


def _parse_skill_frontmatter(content: str) -> tuple[str | None, str | None, tuple[str, ...]]:
    """解析 SKILL.md 顶部 frontmatter 中的 name/description/keywords 元数据。"""

    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, None, ()

    name = None
    description = None
    keywords: tuple[str, ...] = ()
    index = 1
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped == "---":
            break
        if stripped.startswith("name:"):
            name = _unquote_frontmatter_value(stripped.removeprefix("name:").strip())
        elif stripped.startswith("description:"):
            description = _unquote_frontmatter_value(
                stripped.removeprefix("description:").strip(),
            )
        elif stripped.startswith("keywords:"):
            raw_keywords = stripped.removeprefix("keywords:").strip()
            list_items: list[str] = []
            if not raw_keywords:
                lookahead = index + 1
                while lookahead < len(lines):
                    item = lines[lookahead].strip()
                    if item == "---" or (item and not item.startswith("-")):
                        break
                    if item.startswith("-"):
                        list_items.append(item.removeprefix("-").strip())
                    lookahead += 1
                index = lookahead - 1
            keywords = _parse_frontmatter_keywords(raw_keywords, list_items)
        index += 1
    return name or None, description or None, keywords


def _parse_frontmatter_keywords(raw_value: str, list_items: list[str] | None = None) -> tuple[str, ...]:
    """解析 keywords 单行或简单 YAML 列表，避免引入 YAML 依赖。"""

    values = list(list_items or [])
    raw = raw_value.strip()
    if raw:
        if raw.startswith("[") and raw.endswith("]"):
            raw = raw[1:-1]
        values.extend(raw.split(","))
    normalized = []
    seen = set()
    for value in values:
        keyword = _unquote_frontmatter_value(value.strip())
        if not keyword or keyword in seen:
            continue
        normalized.append(keyword)
        seen.add(keyword)
    return tuple(normalized)


def _unquote_frontmatter_value(value: str) -> str:
    """去掉 frontmatter 单行值两侧的简单引号，保持解析逻辑轻量。"""

    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value.strip()


def _skill_source_from_path(source: str, path: str, fallback_name: str) -> SkillSource:
    """从 skill 文件读取展示名和说明；解析失败时退回路径名。"""

    content = open(path, encoding="utf-8").read()
    metadata_name, description, keywords = _parse_skill_frontmatter(content)
    name = metadata_name if _is_valid_skill_name(metadata_name) else fallback_name
    return SkillSource(
        name=name,
        entry_name=fallback_name,
        description=description,
        keywords=keywords,
        source=source,
        path=path,
    )


def _discover_skill_sources(workdir: str) -> list[SkillSource]:
    """按优先级扫描所有 skill 文件，并解析 name/description 元数据。"""

    sources = []

    local_root = _skills_dir(workdir)
    if os.path.isdir(local_root):
        for entry_name in sorted(os.listdir(local_root)):
            if entry_name.startswith("."):
                continue
            if entry_name.endswith(".md"):
                fallback_name = os.path.splitext(entry_name)[0]
                if not _is_valid_skill_name(fallback_name):
                    continue
                path = _validate_path_under(local_root, entry_name)
                if os.path.isfile(path):
                    sources.append(_skill_source_from_path("local", path, fallback_name))
                continue
            if not _is_valid_skill_name(entry_name):
                continue
            path = _validate_path_under(
                local_root,
                entry_name,
                "SKILL.md",
                follow_symlinks=False,
            )
            if os.path.isfile(path):
                sources.append(_skill_source_from_path("local", path, entry_name))

    codex_root = _codex_skills_dir()
    if os.path.isdir(codex_root):
        for dirname in sorted(os.listdir(codex_root)):
            if dirname.startswith(".") or not _is_valid_skill_name(dirname):
                continue
            path = _validate_path_under(
                codex_root,
                dirname,
                "SKILL.md",
                follow_symlinks=False,
            )
            if os.path.isfile(path):
                sources.append(_skill_source_from_path("codex", path, dirname))

    return sources


def _skill_sources(workdir: str, name: str) -> list[SkillSource]:
    """按优先级查找本地和全局两类 skill 来源，支持 frontmatter name 别名。"""

    _validate_skill_name(name)
    sources = [
        source
        for source in _discover_skill_sources(workdir)
        if name in (source.name, source.entry_name)
    ]

    log_event(
        LOGGER,
        logging.DEBUG,
        "skill_sources_resolved",
        skill=name,
        sources=[source.source for source in sources],
    )
    return sources


def _format_skill_info(info: SkillInfo) -> str:
    """把 skill 元信息格式化成人类可读的一行状态。"""

    summary = f"{info.name} ({', '.join(info.sources)})"
    if info.description:
        summary = f"{summary} - {info.description}"
    return summary


def _available_skill_infos(workdir: str) -> list[SkillInfo]:
    """扫描项目本地和全局目录，合并同名 skill 的可用来源。"""

    found: dict[str, list[SkillSource]] = {}
    for source in _discover_skill_sources(workdir):
        found.setdefault(source.name, []).append(source)

    infos = []
    for name in sorted(found):
        sources = found[name]
        if not sources:
            continue
        infos.append(SkillInfo(
            name=name,
            description=sources[0].description,
            sources=tuple(source.source for source in sources),
            selected_source=sources[0].source,
            selected_path=sources[0].path,
        ))
    return infos


def skill_catalog(workdir: str) -> list[SkillCatalogEntry]:
    """生成自动选择 skill 的轻量目录，来源优先级与手工加载保持一致。"""

    catalog = []
    seen: set[str] = set()
    for source in _discover_skill_sources(workdir):
        if source.name in seen:
            continue
        seen.add(source.name)
        title = _read_skill_title(source.path)
        search_parts = [
            source.name,
            source.entry_name,
            source.description or "",
            title or "",
            " ".join(source.keywords),
        ]
        catalog.append(SkillCatalogEntry(
            name=source.name,
            entry_name=source.entry_name,
            keywords=source.keywords,
            search_text="\n".join(part for part in search_parts if part),
        ))
    return catalog


def _read_skill_title(path: str) -> str | None:
    """读取 skill 正文第一个 Markdown 标题，供自动路由解释使用。"""

    try:
        for line in open(path, encoding="utf-8"):
            title = line.strip()
            if title.startswith("# "):
                return title.removeprefix("# ").strip() or None
    except OSError:
        return None
    return None


def list_skills(workdir: str) -> list[str]:
    """列出可用 skill 名称。"""

    return [info.name for info in _available_skill_infos(workdir)]


def _skill_entry_names(workdir: str) -> list[str]:
    """列出可用于调用 skill 的规范名和路径别名。"""

    names = set()
    for source in _discover_skill_sources(workdir):
        names.add(source.name)
        names.add(source.entry_name)
    return sorted(names)


def describe_skills(workdir: str) -> list[str]:
    """列出可用 skill 及其来源，用于 CLI 展示。"""

    return [_format_skill_info(info) for info in _available_skill_infos(workdir)]


def describe_loaded_skills(workdir: str, loaded_skills: list[str]) -> list[str]:
    """描述当前已加载 skill；缺失项也显式标记出来。"""

    loaded = []
    for name in loaded_skills:
        try:
            info = resolve_skill(workdir, name)
            loaded.append(f"{info.name} ({info.selected_source})")
        except FileNotFoundError:
            loaded.append(f"{name} (missing)")
    return loaded


def _print_items(label: str, items: list[str], indent: str = "  ") -> None:
    """向 stderr 打印带缩进的列表。"""

    print(f"{indent}{label}:", file=sys.stderr)
    for item in items:
        print(f"{indent}  {item}", file=sys.stderr)


def print_skill_status(workdir: str, loaded_skills: list[str]) -> None:
    """在非 rich 路径下输出 skill 可用/已加载状态。"""

    avail = describe_skills(workdir)
    if avail:
        _print_items("Available", avail)
    else:
        print(f"  (no skills in {SKILLS_RELPATH}/ or {_codex_skills_dir()}/)", file=sys.stderr)
    if loaded_skills:
        _print_items("Loaded", describe_loaded_skills(workdir, loaded_skills))
    else:
        print("  (no skills loaded)", file=sys.stderr)


def parse_skill_invocation(workdir: str, inp: str) -> SkillInvocation | None:
    """把 `/skill-name prompt` 快捷语法解析成加载并运行 skill 的请求。"""

    if not inp.startswith("/") or inp == "/":
        return None
    command, _, prompt = inp[1:].partition(" ")
    name = command.strip()
    if not name:
        return None
    return SkillInvocation(info=resolve_skill(workdir, name), prompt=prompt.strip())


def resolve_skill(workdir: str, name: str) -> SkillInfo:
    """解析 skill 名称；不存在时返回带候选列表的错误。"""

    sources = _skill_sources(workdir, name)
    if not sources:
        log_event(LOGGER, logging.WARNING, "skill_missing", skill=name, workdir=workdir)
        avail = describe_skills(workdir)
        if avail:
            hint = "Available:\n  " + "\n  ".join(avail)
        else:
            hint = "(no skills yet)"
        raise FileNotFoundError(f"Skill '{name}' not found. {hint}")
    return SkillInfo(
        name=sources[0].name,
        description=sources[0].description,
        sources=tuple(source.source for source in sources),
        selected_source=sources[0].source,
        selected_path=sources[0].path,
    )


def load_skill(workdir: str, name: str) -> tuple[SkillInfo, str]:
    """读取已解析 skill 的文件内容。"""

    info = resolve_skill(workdir, name)
    content = open(info.selected_path, encoding="utf-8").read().strip()
    log_event(
        LOGGER,
        logging.INFO,
        "skill_loaded",
        skill=name,
        source=info.selected_source,
        chars=len(content),
    )
    return info, content


def _skill_system_content(info: SkillInfo, content: str) -> str:
    """把 skill 内容和其文件位置一起注入，明确相对路径解析规则。"""

    skill_dir = os.path.dirname(info.selected_path)
    return (
        f"--- Skill: {info.name} ({info.selected_source}) ---\n"
        f"Skill path: {info.selected_path}\n"
        f"Skill dir: {skill_dir}\n"
        "Relative path rule: resolve paths mentioned by this skill, such as "
        "`scripts/...`, `references/...`, or `assets/...`, relative to Skill dir. "
        "Use the resolved path when calling tools.\n\n"
        f"{content}"
    )


def build_skill_messages(loaded_skills: list[str], workdir: str) -> list[dict[str, str]]:
    """构建 skill system 消息；缺失 skill 静默跳过以兼容旧会话。"""

    messages = []
    for skill_name in loaded_skills:
        try:
            info, content = load_skill(workdir, skill_name)
            messages.append({
                "role": "system",
                "content": _skill_system_content(info, content),
            })
        except FileNotFoundError:
            pass
    return messages
