"""dong CLI：带结构化工具 I/O 和 skill 加载能力的最小编码代理。"""
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from importlib import resources

from dong.log_viewer import LogFilter, stream_logs
from dong.llm import chat, get_model_name
from dong.logging_config import configure_logging, get_logger, log_event
from dong.mcp import McpConfigFile, McpManager, configured_server_names, config_path
from dong.tool import ToolResult
from dong.tools import TOOL_DEFS, execute, _is_dangerous, _validate_path
from dong.ui import TerminalUI

LOGGER = get_logger(__name__)

DEFAULT_AGENT_DEFINE_FILENAME = "default_agent_define.md"
SKILLS_RELPATH = ".dong/skills"
CODEX_SKILLS_RELPATH = "skills"
DONG_RULE_CANDIDATES = ["DONG.md", ".dong/DONG.md"]
CONTEXT_SUMMARY_RELPATH = ".dong/context"
CONTEXT_SUMMARY_PREFIX = "--- Compacted conversation context ---"
DEFAULT_CONTEXT_MAX_MESSAGES = 14
DEFAULT_CONTEXT_MAX_CHARS = 24_000


@lru_cache(maxsize=1)
def load_default_instructions() -> str:
    """从包内 markdown 加载 dong 默认系统提示词，作为 LLM instructions 使用。"""
    return (
        resources.files("dong")
        .joinpath(DEFAULT_AGENT_DEFINE_FILENAME)
        .read_text(encoding="utf-8")
        .strip()
    )


# 兼容旧测试和外部导入；真实请求会把它放入 instructions，而不是 messages。
SYSTEM_PROMPT = load_default_instructions()


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
    source: str
    path: str


@dataclass(frozen=True)
class SkillInvocation:
    """slash skill 调用解析后的结果，包含 skill 元信息和用户 prompt。"""
    info: SkillInfo
    prompt: str


@dataclass(frozen=True)
class ReplAction:
    """REPL 命令处理结果，用于驱动退出、切目录或继续执行 prompt。"""
    handled: bool
    exit_requested: bool = False
    workdir: str | None = None
    prompt: str | None = None


@dataclass(frozen=True)
class AgentPrompt:
    """一次 agent 请求的固定提示词；instructions 是发给 LLM 的系统提示词。"""
    instructions: str
    context_messages: list


# ═══════════════════════════════════════
#  项目级自动提示词
# ═══════════════════════════════════════

def load_dong_md(workdir):
    """从项目根目录或 .dong/ 加载 DONG.md，并转换成系统消息。"""
    for relpath in DONG_RULE_CANDIDATES:
        path = os.path.join(workdir, relpath)
        if os.path.isfile(path):
            content = open(path, encoding="utf-8").read().strip()
            if content:
                log_event(
                    LOGGER,
                    logging.INFO,
                    "dong_rules_loaded",
                    relpath=relpath,
                    chars=len(content),
                )
                return {
                    "role": "system",
                    "content": f"--- Project Rules (DONG.md) ---\n{content}",
                }
    log_event(LOGGER, logging.DEBUG, "dong_rules_not_found", workdir=workdir)
    return None


# ═══════════════════════════════════════
#  Skill 管理
# ═══════════════════════════════════════

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


def _parse_skill_frontmatter(content: str) -> tuple[str | None, str | None]:
    """解析 SKILL.md 顶部 frontmatter 中的 name/description 元数据。"""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, None

    name = None
    description = None
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if stripped.startswith("name:"):
            name = _unquote_frontmatter_value(stripped.removeprefix("name:").strip())
        elif stripped.startswith("description:"):
            description = _unquote_frontmatter_value(
                stripped.removeprefix("description:").strip(),
            )
    return name or None, description or None


def _unquote_frontmatter_value(value: str) -> str:
    """去掉 frontmatter 单行值两侧的简单引号，保持解析逻辑轻量。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value.strip()


def _skill_source_from_path(source: str, path: str, fallback_name: str) -> SkillSource:
    """从 skill 文件读取展示名和说明；解析失败时退回路径名。"""
    content = open(path, encoding="utf-8").read()
    metadata_name, description = _parse_skill_frontmatter(content)
    name = metadata_name if _is_valid_skill_name(metadata_name) else fallback_name
    return SkillSource(
        name=name,
        entry_name=fallback_name,
        description=description,
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


def print_items(label: str, items: list[str], indent: str = "  ") -> None:
    """向 stderr 打印带缩进的列表。"""
    print(f"{indent}{label}:", file=sys.stderr)
    for item in items:
        print(f"{indent}  {item}", file=sys.stderr)


def print_skill_status(workdir: str, loaded_skills: list[str]) -> None:
    """在非 rich 路径下输出 skill 可用/已加载状态。"""
    avail = describe_skills(workdir)
    if avail:
        print_items("Available", avail)
    else:
        print(f"  (no skills in {SKILLS_RELPATH}/ or {_codex_skills_dir()}/)", file=sys.stderr)
    if loaded_skills:
        print_items("Loaded", describe_loaded_skills(workdir, loaded_skills))
    else:
        print("  (no skills loaded)", file=sys.stderr)


def show_skill_status(ui: TerminalUI, workdir: str, loaded_skills: list[str]) -> None:
    """通过 UI 适配层展示 skill 可用/已加载状态。"""
    avail = describe_skills(workdir)
    if avail:
        ui.err_console.print("  Available:")
        for item in avail:
            ui.err_console.print(f"    {item}")
    else:
        ui.err_console.print(f"  (no skills in {SKILLS_RELPATH}/ or {_codex_skills_dir()}/)")
    if loaded_skills:
        ui.err_console.print("  Loaded:")
        for item in describe_loaded_skills(workdir, loaded_skills):
            ui.err_console.print(f"    {item}")
    else:
        ui.err_console.print("  (no skills loaded)")


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


def build_messages(loaded_skills, workdir):
    """构建动态系统消息；默认系统提示词由 instructions 单独承载。"""
    msgs = []
    project_rules = load_dong_md(workdir)
    if project_rules:
        msgs.append(project_rules)
    for sname in loaded_skills:
        try:
            info, content = load_skill(workdir, sname)
            msgs.append({
                "role": "system",
                "content": _skill_system_content(info, content),
            })
        except FileNotFoundError:
            pass
    return msgs


def build_agent_prompt(loaded_skills, workdir) -> AgentPrompt:
    """构建固定 agent prompt，把所有 system 内容集中到 instructions。"""
    return _agent_prompt_from_messages(build_messages(loaded_skills, workdir))


def _agent_prompt_from_messages(messages: list) -> AgentPrompt:
    """把历史 system message 合并进 instructions，其余消息作为 input 上下文。"""
    instruction_parts = [SYSTEM_PROMPT]
    context_messages = []
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "system":
            content = (message.get("content") or "").strip()
            if content:
                instruction_parts.append(content)
        else:
            context_messages.append(message)
    return AgentPrompt(
        instructions="\n\n".join(instruction_parts),
        context_messages=context_messages,
    )


# ═══════════════════════════════════════
#  Agent 主循环
# ═══════════════════════════════════════

def choose(prompt, default=None):
    """读取一个简单 y/n 选择；保留给非 UI 适配路径使用。"""
    suffix = " [Y/n] " if default == "y" else " [y/N] "
    return input(prompt + suffix).strip().lower() in ("y", "yes", "" if default == "y" else "")


def _role(m):
    """兼容 dict 和 ChatCompletionMessage 两种消息结构读取 role。"""
    if isinstance(m, dict):
        return m.get("role", "")
    return getattr(m, "role", "")


def _tool_calls(m):
    """兼容 dict 和 ChatCompletionMessage 两种消息结构读取 tool_calls。"""
    if isinstance(m, dict):
        return m.get("tool_calls") or []
    return getattr(m, "tool_calls") or []


def _message_content(m) -> str:
    """读取消息正文；非字符串结构会压成 JSON，便于统一估算上下文大小。"""
    if isinstance(m, dict):
        content = m.get("content", "")
    else:
        content = getattr(m, "content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _message_tool_call_text(m) -> str:
    """把 assistant 的工具调用名称和参数纳入预算，避免隐藏的大参数撑爆上下文。"""
    parts = []
    for tc in _tool_calls(m):
        if isinstance(tc, dict):
            function = tc.get("function") or {}
            parts.append(str(function.get("name", "")))
            parts.append(str(function.get("arguments", "")))
        else:
            function = tc.function
            parts.append(str(function.name))
            parts.append(str(function.arguments))
    return "\n".join(parts)


def _message_chars(m) -> int:
    """用字符数近似 token 预算；不引入 tokenizer 依赖以保持项目轻量。"""
    return (
        len(_role(m))
        + len(_message_content(m))
        + len(_reasoning_content(m))
        + len(_message_tool_call_text(m))
    )


def _context_chars(messages: list) -> int:
    """估算一组 working 消息的上下文字符体量。"""
    return sum(_message_chars(message) for message in messages)


def _context_max_chars() -> int:
    """读取上下文字符预算；无效配置回退到保守默认值。"""
    raw = os.getenv("DONG_CONTEXT_MAX_CHARS", "").strip()
    if not raw:
        return DEFAULT_CONTEXT_MAX_CHARS
    try:
        return max(1_000, int(raw))
    except ValueError:
        return DEFAULT_CONTEXT_MAX_CHARS


def _tool_call_id(tc) -> str:
    """兼容 dict 和 SDK 对象读取 tool_call id。"""
    return tc.get("id", "") if isinstance(tc, dict) else tc.id


def _tool_result_id(m) -> str | None:
    """读取 tool result 对应的 tool_call_id。"""
    if isinstance(m, dict):
        return m.get("tool_call_id")
    return getattr(m, "tool_call_id", None)


def _safe_tail_start(messages: list, keep_from: int) -> int:
    """把保留边界回退到安全点，避免拆散 assistant tool_call 与 tool result。"""
    k = max(0, min(keep_from, len(messages)))
    while 0 < k < len(messages) and _role(messages[k]) == "tool":
        k -= 1
        if _role(messages[k]) == "assistant":
            break
    return k


def _advance_tail_start(messages: list, keep_from: int, *, min_tail_messages: int = 4) -> int | None:
    """向前移动保留边界；如果落在 tool result 组内，就跳过整个结果组。"""
    k = min(keep_from + 1, len(messages))
    while k < len(messages) and _role(messages[k]) == "tool":
        k += 1
    if len(messages) - k < min_tail_messages:
        return None

    safe_k = _safe_tail_start(messages, k)
    return safe_k if safe_k > keep_from else None


def _drop_orphan_tool_results(messages: list) -> list:
    """丢弃没有对应 assistant tool_call 的孤立 tool 消息，避免 provider 400。"""
    valid_ids: set[str] = set()
    result = []
    for message in messages:
        role = _role(message)
        if role == "assistant":
            for tc in _tool_calls(message):
                tid = _tool_call_id(tc)
                if tid:
                    valid_ids.add(tid)
            result.append(message)
        elif role == "tool":
            tid = _tool_result_id(message)
            if tid in valid_ids:
                result.append(message)
        else:
            result.append(message)
    return result


def _summarize_message(message) -> str:
    """把单条旧消息压成一行摘要，控制压缩消息自身不要继续膨胀。"""
    role = _role(message) or "unknown"
    content = re.sub(r"\s+", " ", _message_content(message)).strip()
    tool_text = _message_tool_call_text(message)
    if tool_text:
        compact_tool_text = re.sub(r"\s+", " ", tool_text).strip()
        content = f"{content} tool_calls={compact_tool_text}"
    if len(content) > 180:
        content = content[:179].rstrip() + "…"
    return f"- {role}: {content}" if content else f"- {role}: <empty>"


def _build_context_summary(removed: list) -> str:
    """生成确定性上下文摘要；不用额外 LLM 调用，确保离线可测试。"""
    counts: dict[str, int] = {}
    tools: set[str] = set()
    for message in removed:
        role = _role(message) or "unknown"
        counts[role] = counts.get(role, 0) + 1
        for tc in _tool_calls(message):
            if isinstance(tc, dict):
                name = (tc.get("function") or {}).get("name")
            else:
                name = tc.function.name
            if name:
                tools.add(str(name))

    recent_user = [
        _summarize_message(message)
        for message in removed
        if _role(message) == "user"
    ][-3:]
    timeline = [_summarize_message(message) for message in removed[-8:]]

    lines = [
        CONTEXT_SUMMARY_PREFIX,
        "说明：以下内容由 dong 在本地根据旧消息自动压缩生成，不依赖数据库或额外 LLM 调用。",
        f"- 压缩消息数：{len(removed)}",
        "- 角色分布：" + ", ".join(f"{role}={count}" for role, count in sorted(counts.items())),
    ]
    if tools:
        lines.append("- 涉及工具：" + ", ".join(sorted(tools)))
    if recent_user:
        lines.append("- 最近用户请求：")
        lines.extend(f"  {line}" for line in recent_user)
    lines.append("- 压缩前尾部时间线：")
    lines.extend(f"  {line}" for line in timeline)
    return "\n".join(lines)


def _write_context_summary(workdir: str, summary: str) -> str:
    """把压缩摘要落盘到项目内 `.dong/context/`，满足文件化留痕要求。"""
    context_dir = _validate_path(workdir, CONTEXT_SUMMARY_RELPATH)
    os.makedirs(context_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"compact-{timestamp}.md"
    relpath = f"{CONTEXT_SUMMARY_RELPATH}/{filename}"
    path = _validate_path(workdir, relpath)
    with open(path, "w", encoding="utf-8") as f:
        f.write(summary)
        f.write("\n")
    return relpath


def trim_context(messages, max_len=DEFAULT_CONTEXT_MAX_MESSAGES, workdir: str | None = None):
    """按消息数和字符预算压缩上下文，同时保留 tool_call/tool 结果配对关系。"""
    max_chars = _context_max_chars()
    if len(messages) <= max_len and _context_chars(messages) <= max_chars:
        return messages

    keep_from = _safe_tail_start(messages, len(messages) - max_len)
    tail = messages[keep_from:]

    # 如果尾部本身仍然过大，逐步减少保留条数，但至少保留最近 4 条上下文。
    while len(tail) > 4 and _context_chars(tail) > max_chars:
        next_keep_from = _advance_tail_start(messages, keep_from)
        if next_keep_from is None:
            break
        keep_from = next_keep_from
        tail = messages[keep_from:]

    removed = messages[:keep_from]
    if not removed:
        return _drop_orphan_tool_results(tail)

    summary = _build_context_summary(removed)
    summary_ref = _write_context_summary(workdir, summary) if workdir else None
    if summary_ref:
        summary = f"{summary}\n- 摘要文件：{summary_ref}"
    summary_message = {
        "role": "system",
        "content": (
            f"{summary}\n\n"
            "Recent messages are preserved verbatim. Continue from this summary without recap."
        ),
    }
    log_event(
        LOGGER,
        logging.INFO,
        "context_compacted",
        removed_messages=len(removed),
        preserved_messages=len(tail),
        estimated_chars_before=_context_chars(messages),
        estimated_chars_after=_context_chars([summary_message, *tail]),
        summary_ref=summary_ref,
    )
    return _drop_orphan_tool_results([summary_message, *tail])


def _reasoning_content(message) -> str:
    """读取 OpenAI 兼容消息里的 DeepSeek reasoning_content。"""
    if isinstance(message, dict):
        value = message.get("reasoning_content")
    else:
        value = getattr(message, "reasoning_content", None)
        if value is None:
            model_extra = getattr(message, "model_extra", None)
            if isinstance(model_extra, dict):
                value = model_extra.get("reasoning_content")

    return value.strip() if isinstance(value, str) else ""


def _start_mcp_manager(workdir: str, ui: TerminalUI) -> McpManager:
    """启动本轮可用的 MCP server；配置错误会降级为禁用 MCP。"""
    try:
        manager = McpManager.from_workdir(workdir)
        manager.start()
        for server, error in manager.startup_errors.items():
            ui.show_warning(f"MCP server {server} failed: {error}")
        return manager
    except Exception as e:
        ui.show_warning(f"MCP disabled: {e}")
        log_event(LOGGER, logging.WARNING, "mcp_disabled", error=type(e).__name__)
        return McpManager(workdir, McpConfigFile())


def _empty_mcp_manager(workdir: str) -> McpManager:
    """返回不启动任何 server 的空 MCP manager。"""
    return McpManager(workdir, McpConfigFile())


def _execute_registered_tool(
    name: str,
    args_raw: str,
    workdir: str,
    mcp_manager: McpManager,
) -> ToolResult:
    """统一执行内置工具或 MCP 工具，保持 agent loop 主流程简单。"""
    if mcp_manager.has_tool(name):
        return mcp_manager.execute(name, args_raw)
    return execute(name, args_raw, workdir)


def _tool_timeout_seconds(name: str, args_raw: str, mcp_manager: McpManager) -> float | None:
    """读取工具的已知超时预算，用于 UI 展示耗时和超时阶段。"""
    if name == "bash":
        return 30.0
    if name == "fetch":
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except json.JSONDecodeError:
            return 15.0
        if isinstance(args, dict):
            timeout = args.get("timeout", 15)
            if isinstance(timeout, int | float) and timeout > 0:
                return float(timeout)
        return 15.0
    if mcp_manager.has_tool(name):
        route = mcp_manager.routes.get(name)
        if route is not None and route.server_name in mcp_manager.clients:
            return mcp_manager.clients[route.server_name].config.tool_timeout_seconds
    return None


def _invalid_tool_input_result(name: str, error: Exception | str) -> ToolResult:
    """把工具参数预检失败降级成 ToolResult，避免 agent loop 直接崩溃。"""
    return ToolResult(success=False, error=f"Invalid input for {name}: {error}")


def _bash_command_from_args(args_raw: str) -> tuple[str | None, ToolResult | None]:
    """安全读取 bash command，供危险命令确认使用。"""
    try:
        args = json.loads(args_raw)
    except Exception as exc:
        return None, _invalid_tool_input_result("bash", exc)
    if not isinstance(args, dict):
        return None, _invalid_tool_input_result("bash", "arguments must be a JSON object")
    command = args.get("command")
    if not isinstance(command, str):
        return None, _invalid_tool_input_result("bash", "command must be a string")
    return command, None


def _append_tool_result(messages: list, tool_call_id: str, result: ToolResult) -> None:
    """把工具结果按 provider 需要的 tool message 形状追加进上下文。"""
    messages.append({
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": result.to_message()["content"],
    })


def _show_task_interrupted(ui: TerminalUI, *, phase: str, tool: str | None = None) -> None:
    """清理当前任务中断的用户可见状态。"""
    ui.blank_line()
    target = f"工具 {tool}" if tool else phase
    ui.show_warning(f"已中断当前任务：{target}")


def _show_task_failed(ui: TerminalUI, *, phase: str, error: Exception) -> None:
    """展示非中断类任务失败，避免裸 traceback 泄漏到 CLI。"""
    message = str(error).strip() or type(error).__name__
    ui.show_error(f"{phase}失败：{type(error).__name__}: {message}")


def _chat_with_streaming_ui(messages, tool_defs, instructions: str, ui: TerminalUI):
    """调用 LLM 时把文本 delta 直接交给 UI，同时保留完整 message 给工具循环。"""
    working_status = ui.show_working("AI 正在思考...")
    working_active = True
    working_entered = False

    try:
        working_status.__enter__()
        working_entered = True
        with ui.stream_assistant_message() as write_delta:

            def on_text_delta(delta: str) -> None:
                nonlocal working_active
                if not delta:
                    return
                if working_active and working_entered:
                    working_status.__exit__(None, None, None)
                    working_active = False
                write_delta(delta)

            message = chat(
                messages,
                tool_defs,
                instructions=instructions,
                on_text_delta=on_text_delta,
            )
    except BaseException as exc:
        if working_active and working_entered:
            working_status.__exit__(type(exc), exc, exc.__traceback__)
            working_active = False
        raise
    finally:
        if working_active and working_entered:
            working_status.__exit__(None, None, None)

    return message


def run_turn(messages, workdir, ui: TerminalUI | None = None, enable_mcp: bool = False):
    """执行单轮 agent 调用；没有工具调用且产生文本时返回完成。"""
    ui = ui or TerminalUI()
    agent_prompt = _agent_prompt_from_messages(messages)
    log_event(
        LOGGER,
        logging.INFO,
        "run_turn_started",
        workdir=workdir,
        messages=len(messages),
    )
    mcp_manager = _start_mcp_manager(workdir, ui) if enable_mcp else _empty_mcp_manager(workdir)
    try:
        tool_defs = TOOL_DEFS + mcp_manager.tool_definitions
        try:
            msg = _chat_with_streaming_ui(
                agent_prompt.context_messages,
                tool_defs,
                agent_prompt.instructions,
                ui,
            )
        except KeyboardInterrupt:
            _show_task_interrupted(ui, phase="AI 思考")
            log_event(LOGGER, logging.WARNING, "run_turn_interrupted", phase="llm")
            return False
        except Exception as exc:
            _show_task_failed(ui, phase="AI 请求", error=exc)
            log_event(
                LOGGER,
                logging.WARNING,
                "run_turn_failed",
                phase="llm",
                error=type(exc).__name__,
            )
            return False
        messages.append(msg)
        reasoning = _reasoning_content(msg)
        if reasoning:
            ui.show_reasoning_message(reasoning)

        for tc in (msg.tool_calls or []):
            name = tc.function.name
            args_raw = tc.function.arguments
            log_event(
                LOGGER,
                logging.INFO,
                "tool_call_received",
                tool=name,
                args_chars=len(args_raw),
            )

            if name == "bash":
                cmd, invalid_result = _bash_command_from_args(args_raw)
                if invalid_result is not None:
                    ui.show_tool_result(name, args_raw, invalid_result)
                    _append_tool_result(messages, tc.id, invalid_result)
                    log_event(
                        LOGGER,
                        logging.WARNING,
                        "tool_call_invalid_input",
                        tool=name,
                        error=invalid_result.error,
                    )
                    continue
                if _is_dangerous(cmd) and not ui.confirm_dangerous_command(cmd, default="n"):
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": "[✗] User cancelled dangerous command",
                    })
                    ui.show_tool_cancelled(name, args_raw)
                    log_event(LOGGER, logging.WARNING, "tool_call_cancelled", tool=name)
                    continue

            try:
                with ui.show_working(
                    f"正在执行工具：{name}",
                    timeout_seconds=_tool_timeout_seconds(name, args_raw, mcp_manager),
                ):
                    result = _execute_registered_tool(name, args_raw, workdir, mcp_manager)
            except KeyboardInterrupt:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": "[✗] User cancelled current task",
                })
                _show_task_interrupted(ui, phase="tool", tool=name)
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "run_turn_interrupted",
                    phase="tool",
                    tool=name,
                )
                return False
            ui.show_tool_result(name, args_raw, result)
            _append_tool_result(messages, tc.id, result)

        if not msg.tool_calls and msg.content:
            # 最终答复需要稳定的 Markdown 面板；流式裸输出只作为等待期间的即时反馈。
            ui.show_assistant_message(msg.content)
            final_content_chars = len(msg.content)
            log_event(
                LOGGER,
                logging.INFO,
                "run_turn_finished",
                final_content_chars=final_content_chars,
                reasoning_chars=len(reasoning),
            )
            return True
        log_event(LOGGER, logging.INFO, "run_turn_waiting_for_tools")
        return False
    finally:
        mcp_manager.close()


def run_loop(
    base_sys,
    working,
    workdir,
    max_turns=200,
    ui: TerminalUI | None = None,
    enable_mcp: bool = False,
):
    """执行多轮 agent 循环，直到模型给出最终文本或达到轮数上限。"""
    ui = ui or TerminalUI()
    agent_prompt = base_sys if isinstance(base_sys, AgentPrompt) else _agent_prompt_from_messages(base_sys)
    log_event(
        LOGGER,
        logging.INFO,
        "run_loop_started",
        workdir=workdir,
        max_turns=max_turns,
        base_messages=len(agent_prompt.context_messages),
        working_messages=len(working),
        instructions_chars=len(agent_prompt.instructions),
    )
    mcp_manager = _start_mcp_manager(workdir, ui) if enable_mcp else _empty_mcp_manager(workdir)
    try:
        tool_defs = TOOL_DEFS + mcp_manager.tool_definitions
        # Agent 主循环：模型可以多轮思考、调用工具、读取工具结果，再继续下一轮。
        for turn in range(max_turns):
            # 每轮都用固定 input 上下文 + 当前对话上下文，系统提示词单独走 instructions。
            messages = agent_prompt.context_messages + working
            log_event(
                LOGGER,
                logging.DEBUG,
                "run_loop_turn_started",
                turn=turn + 1,
                messages=len(messages),
            )
            try:
                msg = _chat_with_streaming_ui(
                    messages,
                    tool_defs,
                    agent_prompt.instructions,
                    ui,
                )
            except KeyboardInterrupt:
                _show_task_interrupted(ui, phase="AI 思考")
                log_event(LOGGER, logging.WARNING, "run_loop_interrupted", phase="llm")
                return
            except Exception as exc:
                _show_task_failed(ui, phase="AI 请求", error=exc)
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "run_loop_failed",
                    phase="llm",
                    error=type(exc).__name__,
                    turn=turn + 1,
                )
                return

            # 先把模型本轮返回写入上下文；如果它请求工具调用，后面会继续追加工具结果。
            working.append(msg)
            reasoning = _reasoning_content(msg)
            if reasoning:
                ui.show_reasoning_message(reasoning)

            for tc in (msg.tool_calls or []):
                name = tc.function.name
                args_raw = tc.function.arguments
                log_event(
                    LOGGER,
                    logging.INFO,
                    "tool_call_received",
                    tool=name,
                    args_chars=len(args_raw),
                    turn=turn + 1,
                )

                if name == "bash":
                    # bash 工具风险最高，所以执行前先解析 command 并做危险命令确认。
                    cmd, invalid_result = _bash_command_from_args(args_raw)
                    if invalid_result is not None:
                        ui.show_tool_result(name, args_raw, invalid_result)
                        _append_tool_result(working, tc.id, invalid_result)
                        log_event(
                            LOGGER,
                            logging.WARNING,
                            "tool_call_invalid_input",
                            tool=name,
                            error=invalid_result.error,
                            turn=turn + 1,
                        )
                        continue
                    if _is_dangerous(cmd) and not ui.confirm_dangerous_command(cmd, default="n"):
                        # 用户拒绝危险命令时，也要把拒绝结果写回上下文，让模型知道发生了什么。
                        working.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": "[✗] User cancelled dangerous command",
                        })
                        ui.show_tool_cancelled(name, args_raw)
                        log_event(LOGGER, logging.WARNING, "tool_call_cancelled", tool=name)
                        continue

                # 统一执行内置工具或 MCP 工具，并把结果追加回上下文。
                try:
                    with ui.show_working(
                        f"正在执行工具：{name}",
                        timeout_seconds=_tool_timeout_seconds(name, args_raw, mcp_manager),
                    ):
                        result = _execute_registered_tool(name, args_raw, workdir, mcp_manager)
                except KeyboardInterrupt:
                    working.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": "[✗] User cancelled current task",
                    })
                    _show_task_interrupted(ui, phase="tool", tool=name)
                    log_event(
                        LOGGER,
                        logging.WARNING,
                        "run_loop_interrupted",
                        phase="tool",
                        tool=name,
                        turn=turn + 1,
                    )
                    return
                ui.show_tool_result(name, args_raw, result)

                # 工具结果必须作为 tool message 追加回上下文，模型下一轮才能基于结果继续判断。
                _append_tool_result(working, tc.id, result)

            # 没有工具调用且有文本内容，表示模型已经给出最终答复，本次任务结束。
            if not msg.tool_calls and msg.content:
                # 最终答复需要稳定补渲染，避免 stdout/stderr 流式输出和 spinner 抢行后看不到总结。
                ui.show_assistant_message(msg.content)
                log_event(
                    LOGGER,
                    logging.INFO,
                    "run_loop_finished",
                    turns=turn + 1,
                    final_content_chars=len(msg.content),
                    reasoning_chars=len(reasoning),
                )
                return

            # 控制上下文长度，避免长时间 REPL 或多轮工具调用导致 messages 过大。
            working[:] = trim_context(working, workdir=workdir)
    finally:
        mcp_manager.close()

    ui.show_warning("Max turns reached. Stopping.")
    log_event(LOGGER, logging.WARNING, "run_loop_max_turns", max_turns=max_turns)


def repl_completions(workdir: str, loaded_skills: list[str]) -> list[str]:
    """生成 REPL 自动补全候选，包括固定命令和当前可用 skill。"""
    commands = [
        "exit",
        "quit",
        "/bye",
        "clear",
        "dir=",
        "/skill",
        "/skills",
        "/unskill",
    ]
    skills = list_skills(workdir)
    skill_entries = _skill_entry_names(workdir)
    commands.extend(f"/skill {name}" for name in skills)
    commands.extend(f"/{name}" for name in skill_entries)
    commands.extend(name for name in skill_entries)  # 裸 skill 名也支持自动补全
    commands.extend(f"/unskill {name}" for name in loaded_skills)
    return commands


def handle_repl_command(
    inp: str,
    *,
    workdir: str,
    loaded_skills: list[str],
    working: list,
    ui: TerminalUI,
) -> ReplAction:
    """处理 REPL 内置命令，普通用户输入会返回 handled=False。"""
    if inp in ("exit", "quit", "/bye"):
        log_event(LOGGER, logging.INFO, "repl_exit_requested", command=inp)
        return ReplAction(handled=True, exit_requested=True)

    if inp == "clear":
        working.clear()
        ui.show_context_cleared()
        log_event(LOGGER, logging.INFO, "repl_context_cleared")
        return ReplAction(handled=True)

    if inp.startswith("dir="):
        new_workdir = os.path.abspath(inp[4:])
        if not os.path.isdir(new_workdir):
            ui.show_error(f"工作目录不存在或不是目录：{new_workdir}")
            log_event(
                LOGGER,
                logging.WARNING,
                "repl_workdir_invalid",
                workdir=new_workdir,
            )
            return ReplAction(handled=True)
        ui.show_workdir(new_workdir)
        log_event(LOGGER, logging.INFO, "repl_workdir_changed", workdir=new_workdir)
        return ReplAction(handled=True, workdir=new_workdir)

    if inp in ("/skill", "/skills"):
        show_skill_status(ui, workdir, loaded_skills)
        log_event(LOGGER, logging.INFO, "repl_skill_status_shown")
        return ReplAction(handled=True)

    if inp.startswith("/skill "):
        name = inp[7:].strip()
        try:
            info, _ = load_skill(workdir, name)
            if info.name not in loaded_skills:
                loaded_skills.append(info.name)
            ui.show_loaded_skill(info.name, info.selected_source)
            log_event(LOGGER, logging.INFO, "repl_skill_enabled", skill=info.name)
        except FileNotFoundError as e:
            ui.show_skill_error(e)
            log_event(LOGGER, logging.WARNING, "repl_skill_enable_failed", skill=name)
        return ReplAction(handled=True)

    if inp.startswith("/unskill ") or inp == "/unskill":
        name = inp[9:].strip()
        if not name:
            if loaded_skills:
                show_skill_status(ui, workdir, loaded_skills)
                ui.err_console.print("  Usage: /unskill <name>")
            else:
                ui.err_console.print("  (no skills loaded)")
            return ReplAction(handled=True)
        if name in loaded_skills:
            loaded_skills.remove(name)
            ui.show_removed_skill(name)
            log_event(LOGGER, logging.INFO, "repl_skill_disabled", skill=name)
        else:
            ui.show_skill_not_loaded(name)
            log_event(LOGGER, logging.WARNING, "repl_skill_disable_missing", skill=name)
        return ReplAction(handled=True)

    if inp.startswith("/"):
        command = inp[1:].split(maxsplit=1)[0]
        try:
            invocation = parse_skill_invocation(workdir, inp)
        except FileNotFoundError:
            ui.show_unknown_command_or_skill(command)
            log_event(LOGGER, logging.WARNING, "repl_unknown_slash", command=command)
            return ReplAction(handled=True)
        if invocation is None:
            ui.show_unknown_command_or_skill(command)
            log_event(LOGGER, logging.WARNING, "repl_unknown_slash", command=command)
            return ReplAction(handled=True)
        name = invocation.info.name
        if name not in loaded_skills:
            loaded_skills.append(name)
            ui.show_loaded_skill(name, invocation.info.selected_source)
        else:
            ui.show_skill_already_loaded(name, invocation.info.selected_source)
        log_event(
            LOGGER,
            logging.INFO,
            "repl_skill_invoked",
            skill=name,
            prompt_chars=len(invocation.prompt),
        )
        return ReplAction(handled=True, prompt=invocation.prompt or None)

    # ── 自动识别 skill 名称（不带 / 前缀） ──
    # 用户输入以可用 skill 名称开头时，自动路由为 skill 调用，与 `/name prompt` 等效。
    first_word = inp.split(maxsplit=1)[0] if inp else ""
    if first_word:
        avail_set = set(_skill_entry_names(workdir))
        if first_word in avail_set:
            name = first_word
            prompt = inp[len(name):].strip()
            try:
                info = resolve_skill(workdir, name)
                if info.name not in loaded_skills:
                    loaded_skills.append(info.name)
                    ui.show_loaded_skill(info.name, info.selected_source)
                log_event(
                    LOGGER,
                    logging.INFO,
                    "repl_skill_invoked",
                    skill=info.name,
                    prompt_chars=len(prompt),
                )
                return ReplAction(handled=True, prompt=prompt or None)
            except FileNotFoundError:
                pass

    return ReplAction(handled=False)


def _positive_int(value: str) -> int:
    """解析正整数 CLI 参数，用于 logs limit。"""
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("must be positive")
    return parsed


def _positive_float(value: str) -> float:
    """解析正浮点 CLI 参数，用于 logs follow 轮询间隔。"""
    parsed = float(value)
    if parsed <= 0:
        raise ValueError("must be positive")
    return parsed


def run_logs_cli(argv: list[str]) -> None:
    """执行 `dong logs` 本地日志查看命令。"""
    import argparse

    parser = argparse.ArgumentParser(description="dong logs — view local dong logs")
    parser.add_argument("-d", "--dir", default=".", help="Working directory")
    parser.add_argument("--file", help="Log file path under working directory")
    parser.add_argument("--limit", type=_positive_int, default=200, help="Max matching lines")
    parser.add_argument("--level", help="Filter by level, e.g. INFO or WARNING")
    parser.add_argument("--event", help="Filter by exact event name")
    parser.add_argument("--logger", help="Filter by exact logger name, e.g. dong.tools")
    parser.add_argument("--contains", help="Filter by raw line substring")
    parser.add_argument("--json", action="store_true", help="Emit JSON lines")
    parser.add_argument("--follow", action="store_true", help="Follow appended log lines")
    parser.add_argument("--interval", type=_positive_float, default=1.0, help="Follow interval seconds")
    args = parser.parse_args(argv)

    workdir = os.path.abspath(args.dir)
    stream_logs(
        workdir,
        file_path=args.file,
        limit=args.limit,
        criteria=LogFilter(
            level=args.level,
            event=args.event,
            logger=args.logger,
            contains=args.contains,
        ),
        json_mode=args.json,
        follow=args.follow,
        interval=args.interval,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def run_mcp_cli(argv: list[str]) -> None:
    """执行 `dong mcp` 本地 MCP 查看命令。"""
    import argparse

    parser = argparse.ArgumentParser(description="dong mcp — inspect project MCP servers")
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list", help="List configured MCP servers and tools")
    list_parser.add_argument("-d", "--dir", default=".", help="Working directory")
    list_parser.add_argument(
        "--tools",
        action="store_true",
        help="Connect to enabled servers and list discovered tools",
    )
    args = parser.parse_args(argv)

    if args.command == "list":
        workdir = os.path.abspath(args.dir)
        names = configured_server_names(workdir)
        if not names:
            print(f"(no MCP servers configured in {config_path(workdir)})")
            return
        if not args.tools:
            for name in names:
                print(f"{name}\tconfigured")
            print("Use `dong mcp list --tools` to connect and discover tools.")
            return
        manager = McpManager.from_workdir(workdir)
        manager.start()
        try:
            for name in names:
                status = "connected" if name in manager.clients else "failed"
                print(f"{name}\t{status}")
                for tool_name, route in sorted(manager.routes.items()):
                    if route.server_name == name:
                        print(f"  {tool_name}")
                if name in manager.startup_errors:
                    print(f"  error: {manager.startup_errors[name]}")
        finally:
            manager.close()


# ═══════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════

def main():
    """CLI 主入口：解析参数，并根据是否传入 prompt 选择单次或 REPL 模式。"""
    import argparse

    if len(sys.argv) > 1 and sys.argv[1] == "logs":
        run_logs_cli(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "mcp":
        run_mcp_cli(sys.argv[2:])
        return

    # 解析 CLI 参数：模型、工作目录、最大轮数、预加载 skill、一次性 prompt。
    parser = argparse.ArgumentParser(description="dong — a minimal CLI coding agent")
    parser.add_argument("-m", "--model", default=get_model_name(), help="LLM model")
    parser.add_argument("-d", "--dir", default=".", help="Working directory")
    parser.add_argument("-t", "--max-turns", type=int, default=200,
                        help="Max agent turns before giving up (default: 200)")
    parser.add_argument("--skill", action="append", default=[], help="Load a skill by name")
    parser.add_argument("--mcp", action="store_true", help="Enable configured stdio MCP servers")
    parser.add_argument("input", nargs="*", help="Single prompt. Omit for REPL.")
    args = parser.parse_args()
    ui = TerminalUI()

    # 把模型放到环境变量里，后续 LLM 调用可以统一读取 DONG_MODEL。
    os.environ["DONG_MODEL"] = args.model

    # 统一转成绝对路径，避免后续文件工具在相对路径上产生歧义。
    workdir = os.path.abspath(args.dir)
    log_path = configure_logging(workdir)
    log_event(
        LOGGER,
        logging.INFO,
        "cli_started",
        model=args.model,
        workdir=workdir,
        mode="single" if args.input else "repl",
        log_path=str(log_path) if log_path else None,
    )

    # 加载项目规则；DONG.md 会进入系统消息，约束 agent 的行为。
    project_rules = load_dong_md(workdir)
    ui.show_startup(
        model=args.model,
        workdir=workdir,
        agents_loaded=project_rules is not None,
        tools=[
            *(tool["function"]["name"] for tool in TOOL_DEFS),
            *(["mcp"] if args.mcp else []),
        ],
    )

    # 保存当前已启用的 skill 名称；build_messages 会根据它们拼装系统提示词。
    loaded_skills = []

    # 处理命令行里重复传入的 --skill，例如：--skill python --skill git。
    for name in args.skill:
        try:
            info, _ = load_skill(workdir, name)
            if info.name not in loaded_skills:
                loaded_skills.append(info.name)
            ui.show_loaded_skill(info.name, info.selected_source, indent="   ")
        except FileNotFoundError as e:
            ui.show_skill_error(e)
            raise SystemExit(2) from e

    if args.input:
        # 单次执行模式：命令行里带了 prompt，跑完一轮 run_loop 后直接退出。
        working = []
        user_prompt = " ".join(args.input)
        log_event(LOGGER, logging.INFO, "single_prompt_received", prompt_chars=len(user_prompt))
        working.append({"role": "user", "content": user_prompt})
        base_sys = build_agent_prompt(loaded_skills, workdir)
        run_loop(base_sys, working, workdir, max_turns=args.max_turns, ui=ui, enable_mcp=args.mcp)
    else:
        # REPL 模式：没有一次性 prompt，就进入交互循环，持续保留 working 上下文。
        avail = describe_skills(workdir)
        ui.show_repl_help(skill_count=len(avail))
        log_event(LOGGER, logging.INFO, "repl_started", skill_count=len(avail))
        working = []
        while True:
            try:
                # 每次读取用户输入，直到 EOF/Ctrl-C 或退出命令。
                inp = ui.read_prompt(repl_completions(workdir, loaded_skills))
            except (EOFError, KeyboardInterrupt):
                ui.blank_line()
                break
            inp = inp.strip()
            if not inp:
                continue

            action = handle_repl_command(
                inp,
                workdir=workdir,
                loaded_skills=loaded_skills,
                working=working,
                ui=ui,
            )
            if action.exit_requested:
                break
            if action.workdir is not None:
                workdir = action.workdir
                continue
            if action.prompt is not None:
                working.append({"role": "user", "content": action.prompt})
                base_sys = build_agent_prompt(loaded_skills, workdir)
                run_loop(base_sys, working, workdir, max_turns=args.max_turns, ui=ui, enable_mcp=args.mcp)
                working = trim_context(working, workdir=workdir)
                continue
            if action.handled:
                continue

            # ── 正常对话 ──
            # 普通输入直接进入上下文，并用当前 workdir + skills 重新构建系统消息后运行。
            log_event(LOGGER, logging.INFO, "repl_prompt_received", prompt_chars=len(inp))
            working.append({"role": "user", "content": inp})
            base_sys = build_agent_prompt(loaded_skills, workdir)
            run_loop(base_sys, working, workdir, max_turns=args.max_turns, ui=ui, enable_mcp=args.mcp)
            working = trim_context(working, workdir=workdir)


if __name__ == "__main__":
    main()
