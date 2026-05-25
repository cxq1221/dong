"""dong CLI：带结构化工具 I/O 和 skill 加载能力的最小编码代理。"""
import json
import logging
import os
import sys
from dataclasses import dataclass

from dong.log_viewer import LogFilter, stream_logs
from dong.llm import chat, get_model
from dong.logging_config import configure_logging, get_logger, log_event
from dong.tool import ToolResult
from dong.tools import TOOL_DEFS, execute, _is_dangerous
from dong.ui import TerminalUI

LOGGER = get_logger(__name__)

SYSTEM_PROMPT = """You are dong, a coding agent assistant.

Use the registered tools and discovered skills when you need to read, edit, run commands, fetch data, verify results, or complete related work.

⚠️：Always answer in Chinese, Unless instructed otherwise. 

Core behavior:
- Do One Thing at a time, and do it exactly.
- Keep it Simple. Avoid unnecessary steps, complexity, or abstractions. 
- Think step by step. Provide only the useful answer or requested artifact.
- Read relevant files before editing them. Never claim file contents without reading them first.
- If you get an error, read it and diagnose the cause before trying a different tactic.
- After a tool call, use the tool result as evidence for the next step; do not repeat the same tool call unless the previous result was insufficient.
- Keep changes tightly scoped to the user's request. Do not add speculative abstractions, compatibility shims, unrelated cleanup, or extra features.
- Do not create files unless they are required to complete the task.
- Be careful not to introduce command injection or unsafe shell behavior.
- Prefer local, reversible actions. Ask before destructive, irreversible, external, or high-blast-radius actions.
- Report verification honestly. If tests or checks fail, or were not run, say so explicitly.

Tool results are structured as JSON. When you see [✓] the tool succeeded; when you see [✗] it failed and you should diagnose the error.

Project rules and loaded skills may add narrower instructions. Follow them when they do not conflict with higher-priority system behavior or the user's current intent.

When JSON Output is enabled, return one valid JSON object only. Otherwise, when done, provide a concise summary of what changed and what was verified.
"""

SKILLS_RELPATH = ".dong/skills"
CODEX_SKILLS_RELPATH = "skills"
AGENTS_CANDIDATES = ["DONG.md", ".dong/AGENTS.md"]


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


# ═══════════════════════════════════════
#  AGENTS.md — 项目级自动提示词
# ═══════════════════════════════════════

def load_agents_md(workdir):
    """从项目根目录或 .dong/ 加载 AGENTS.md，并转换成系统消息。"""
    for relpath in AGENTS_CANDIDATES:
        path = os.path.join(workdir, relpath)
        if os.path.isfile(path):
            content = open(path, encoding="utf-8").read().strip()
            if content:
                log_event(
                    LOGGER,
                    logging.INFO,
                    "agents_loaded",
                    relpath=relpath,
                    chars=len(content),
                )
                return {
                    "role": "system",
                    "content": f"--- Project Rules (AGENTS.md) ---\n{content}",
                }
    log_event(LOGGER, logging.DEBUG, "agents_not_found", workdir=workdir)
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


def build_messages(loaded_skills, workdir):
    """构建基础消息：系统提示词、AGENTS.md 和已加载 skill 上下文。"""
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    agents = load_agents_md(workdir)
    if agents:
        msgs.append(agents)
    for sname in loaded_skills:
        try:
            info, content = load_skill(workdir, sname)
            msgs.append({
                "role": "system",
                "content": f"--- Skill: {info.name} ({info.selected_source}) ---\n{content}",
            })
        except FileNotFoundError:
            pass
    return msgs


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


def trim_context(messages, max_len=14):
    """裁剪上下文，同时保留 tool_call 和 tool 结果的配对关系。"""
    if len(messages) <= max_len:
        return messages

    trimmed = messages[-max_len:]

    # 正向扫描收集仍在上下文里的 tool_call_id，并丢弃孤立工具结果。
    valid_ids = set()
    result = []
    for m in trimmed:
        role = _role(m)
        if role == "assistant":
            for tc in _tool_calls(m):
                tid = tc.id if isinstance(tc, dict) else tc.id
                valid_ids.add(tid)
            result.append(m)
        elif role == "tool":
            tid = m.get("tool_call_id") if isinstance(m, dict) else None
            if tid in valid_ids:
                result.append(m)
        else:
            result.append(m)

    return result


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


def run_turn(messages, workdir, ui: TerminalUI | None = None):
    """执行单轮 agent 调用；没有工具调用且产生文本时返回完成。"""
    ui = ui or TerminalUI()
    log_event(
        LOGGER,
        logging.INFO,
        "run_turn_started",
        workdir=workdir,
        messages=len(messages),
    )
    msg = chat(messages, TOOL_DEFS)
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
            cmd = json.loads(args_raw)["command"]
            if _is_dangerous(cmd) and not ui.confirm_dangerous_command(cmd, default="n"):
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": "[✗] User cancelled dangerous command",
                })
                ui.show_tool_cancelled(name, args_raw)
                log_event(LOGGER, logging.WARNING, "tool_call_cancelled", tool=name)
                continue

        result: ToolResult = execute(name, args_raw, workdir)
        ui.show_tool_result(name, args_raw, result)
        messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": result.to_message()["content"],
        })

    if not msg.tool_calls and msg.content:
        ui.show_assistant_message(msg.content)
        log_event(
            LOGGER,
            logging.INFO,
            "run_turn_finished",
            final_content_chars=len(msg.content),
            reasoning_chars=len(reasoning),
        )
        return True
    log_event(LOGGER, logging.INFO, "run_turn_waiting_for_tools")
    return False


def run_loop(base_sys, working, workdir, max_turns=200, ui: TerminalUI | None = None):
    """执行多轮 agent 循环，直到模型给出最终文本或达到轮数上限。"""
    ui = ui or TerminalUI()
    log_event(
        LOGGER,
        logging.INFO,
        "run_loop_started",
        workdir=workdir,
        max_turns=max_turns,
        base_messages=len(base_sys),
        working_messages=len(working),
    )
    # Agent 主循环：模型可以多轮思考、调用工具、读取工具结果，再继续下一轮。
    for turn in range(max_turns):
        # 每轮都用固定系统消息 + 当前对话上下文，组装成发给模型的完整 messages。
        messages = base_sys + working
        log_event(
            LOGGER,
            logging.DEBUG,
            "run_loop_turn_started",
            turn=turn + 1,
            messages=len(messages),
        )
        msg = chat(messages, TOOL_DEFS)

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
                cmd = json.loads(args_raw)["command"]
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

            # 统一执行工具；execute 会根据工具名分发到 read/write/edit/bash/grep/fetch 等实现。
            result: ToolResult = execute(name, args_raw, workdir)
            ui.show_tool_result(name, args_raw, result)

            # 工具结果必须作为 tool message 追加回上下文，模型下一轮才能基于结果继续判断。
            working.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result.to_message()["content"],
            })

        # 没有工具调用且有文本内容，表示模型已经给出最终答复，本次任务结束。
        if not msg.tool_calls and msg.content:
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
        working = trim_context(working)

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


# ═══════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════

def main():
    """CLI 主入口：解析参数，并根据是否传入 prompt 选择单次或 REPL 模式。"""
    import argparse

    if len(sys.argv) > 1 and sys.argv[1] == "logs":
        run_logs_cli(sys.argv[2:])
        return

    # 解析 CLI 参数：模型、工作目录、最大轮数、预加载 skill、一次性 prompt。
    parser = argparse.ArgumentParser(description="dong — a minimal CLI coding agent")
    parser.add_argument("-m", "--model", default=get_model(), help="LLM model")
    parser.add_argument("-d", "--dir", default=".", help="Working directory")
    parser.add_argument("-t", "--max-turns", type=int, default=200,
                        help="Max agent turns before giving up (default: 200)")
    parser.add_argument("--skill", action="append", default=[], help="Load a skill by name")
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

    # 加载项目规则；AGENTS.md 会进入系统消息，约束 agent 的行为。
    agents = load_agents_md(workdir)
    ui.show_startup(
        model=args.model,
        workdir=workdir,
        agents_loaded=agents is not None,
        tools=["read", "write", "edit", "bash", "grep", "fetch"],
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
        base_sys = build_messages(loaded_skills, workdir)
        run_loop(base_sys, working, workdir, max_turns=args.max_turns, ui=ui)
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
                base_sys = build_messages(loaded_skills, workdir)
                run_loop(base_sys, working, workdir, max_turns=args.max_turns, ui=ui)
                working = trim_context(working)
                continue
            if action.handled:
                continue

            # ── 正常对话 ──
            # 普通输入直接进入上下文，并用当前 workdir + skills 重新构建系统消息后运行。
            log_event(LOGGER, logging.INFO, "repl_prompt_received", prompt_chars=len(inp))
            working.append({"role": "user", "content": inp})
            base_sys = build_messages(loaded_skills, workdir)
            run_loop(base_sys, working, workdir, max_turns=args.max_turns, ui=ui)
            working = trim_context(working)


if __name__ == "__main__":
    main()
