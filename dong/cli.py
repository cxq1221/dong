"""dong — a minimal CLI coding agent with structured I/O and skills."""
import json
import os
import sys
from dataclasses import dataclass

from dong.ui import TerminalUI
from dong.tools import TOOL_DEFS, execute, ToolResult, _is_dangerous
from dong.llm import chat, get_model

SYSTEM_PROMPT = """You are dong, a coding agent assistant.

You have tools: read, write, edit, bash, grep, fetch.
- Think step by step. Use tools to read, edit, and verify.
- When done, provide a summary of what you changed.
- Never lie about file contents — read them first.
- If you get an error, read it and try to fix it.

Tool results are structured as JSON. When you see [✓] the tool succeeded,
when you see [✗] it failed and you should diagnose the error.
"""

SKILLS_RELPATH = ".dong/skills"
CODEX_SKILLS_RELPATH = "skills"
AGENTS_CANDIDATES = ["AGENTS.md", ".dong/AGENTS.md"]


@dataclass(frozen=True)
class SkillInfo:
    name: str
    sources: tuple[str, ...]
    selected_source: str
    selected_path: str


@dataclass(frozen=True)
class SkillInvocation:
    info: SkillInfo
    prompt: str


@dataclass(frozen=True)
class ReplAction:
    handled: bool
    exit_requested: bool = False
    workdir: str | None = None
    prompt: str | None = None


# ═══════════════════════════════════════
#  AGENTS.md — 项目级自动提示词
# ═══════════════════════════════════════

def load_agents_md(workdir):
    """Load AGENTS.md from project root or .dong/. Returns system message or None."""
    for relpath in AGENTS_CANDIDATES:
        path = os.path.join(workdir, relpath)
        if os.path.isfile(path):
            content = open(path, encoding="utf-8").read().strip()
            if content:
                return {
                    "role": "system",
                    "content": f"--- Project Rules (AGENTS.md) ---\n{content}",
                }
    return None


# ═══════════════════════════════════════
#  Skill 管理
# ═══════════════════════════════════════

def _skills_dir(workdir: str) -> str:
    return os.path.join(workdir, SKILLS_RELPATH)


def _codex_skills_dir() -> str:
    codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.join(os.path.abspath(codex_home), CODEX_SKILLS_RELPATH)


def _validate_skill_name(name: str) -> None:
    if not name or name != os.path.basename(name) or name in (".", ".."):
        raise FileNotFoundError(f"Invalid skill name: {name!r}")


def _validate_path_under(root: str, *parts: str, follow_symlinks: bool = True) -> str:
    resolve = os.path.realpath if follow_symlinks else os.path.abspath
    root_path = resolve(root)
    path = resolve(os.path.join(root_path, *parts))
    if path != root_path and not path.startswith(root_path + os.sep):
        raise PermissionError(f"Path traversal denied: {os.path.join(*parts)} → {path}")
    return path


def _skill_sources(workdir: str, name: str) -> list[tuple[str, str]]:
    _validate_skill_name(name)
    sources = []

    local_root = _skills_dir(workdir)
    local_path = _validate_path_under(local_root, f"{name}.md")
    if os.path.isfile(local_path):
        sources.append(("local", local_path))

    codex_root = _codex_skills_dir()
    codex_path = _validate_path_under(
        codex_root,
        name,
        "SKILL.md",
        follow_symlinks=False,
    )
    if os.path.isfile(codex_path):
        sources.append(("codex", codex_path))

    return sources


def _format_skill_info(info: SkillInfo) -> str:
    return f"{info.name} ({', '.join(info.sources)})"


def _available_skill_infos(workdir: str) -> list[SkillInfo]:
    found = {}

    local_root = _skills_dir(workdir)
    if os.path.isdir(local_root):
        for filename in os.listdir(local_root):
            if filename.endswith(".md"):
                name = os.path.splitext(filename)[0]
                found.setdefault(name, set()).add("local")

    codex_root = _codex_skills_dir()
    if os.path.isdir(codex_root):
        for name in os.listdir(codex_root):
            if name.startswith("."):
                continue
            skill_path = _validate_path_under(
                codex_root,
                name,
                "SKILL.md",
                follow_symlinks=False,
            )
            if os.path.isfile(skill_path):
                found.setdefault(name, set()).add("codex")

    infos = []
    for name in sorted(found):
        sources = _skill_sources(workdir, name)
        if not sources:
            continue
        infos.append(SkillInfo(
            name=name,
            sources=tuple(source for source, _ in sources),
            selected_source=sources[0][0],
            selected_path=sources[0][1],
        ))
    return infos


def list_skills(workdir: str) -> list[str]:
    return [info.name for info in _available_skill_infos(workdir)]


def describe_skills(workdir: str) -> list[str]:
    return [_format_skill_info(info) for info in _available_skill_infos(workdir)]


def describe_loaded_skills(workdir: str, loaded_skills: list[str]) -> list[str]:
    loaded = []
    for name in loaded_skills:
        try:
            info = resolve_skill(workdir, name)
            loaded.append(f"{name} ({info.selected_source})")
        except FileNotFoundError:
            loaded.append(f"{name} (missing)")
    return loaded


def print_items(label: str, items: list[str], indent: str = "  ") -> None:
    print(f"{indent}{label}:", file=sys.stderr)
    for item in items:
        print(f"{indent}  {item}", file=sys.stderr)


def print_skill_status(workdir: str, loaded_skills: list[str]) -> None:
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
    """Parse `/skill-name prompt` shorthand into a load-and-run invocation."""
    if not inp.startswith("/") or inp == "/":
        return None
    command, _, prompt = inp[1:].partition(" ")
    name = command.strip()
    if not name:
        return None
    return SkillInvocation(info=resolve_skill(workdir, name), prompt=prompt.strip())


def resolve_skill(workdir: str, name: str) -> SkillInfo:
    sources = _skill_sources(workdir, name)
    if not sources:
        avail = describe_skills(workdir)
        if avail:
            hint = "Available:\n  " + "\n  ".join(avail)
        else:
            hint = "(no skills yet)"
        raise FileNotFoundError(f"Skill '{name}' not found. {hint}")
    return SkillInfo(
        name=name,
        sources=tuple(source for source, _ in sources),
        selected_source=sources[0][0],
        selected_path=sources[0][1],
    )


def load_skill(workdir: str, name: str) -> tuple[SkillInfo, str]:
    info = resolve_skill(workdir, name)
    content = open(info.selected_path, encoding="utf-8").read().strip()
    return info, content


def build_messages(loaded_skills, workdir):
    """Build base messages: system + AGENTS.md + skill contexts."""
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    agents = load_agents_md(workdir)
    if agents:
        msgs.append(agents)
    for sname in loaded_skills:
        try:
            info, content = load_skill(workdir, sname)
            msgs.append({
                "role": "system",
                "content": f"--- Skill: {sname} ({info.selected_source}) ---\n{content}",
            })
        except FileNotFoundError:
            pass
    return msgs


# ═══════════════════════════════════════
#  Agent 主循环
# ═══════════════════════════════════════

def choose(prompt, default=None):
    suffix = " [Y/n] " if default == "y" else " [y/N] "
    return input(prompt + suffix).strip().lower() in ("y", "yes", "" if default == "y" else "")


def _role(m):
    """Get role from either a dict or ChatCompletionMessage."""
    if isinstance(m, dict):
        return m.get("role", "")
    return getattr(m, "role", "")


def _tool_calls(m):
    """Get tool_calls from either a dict or ChatCompletionMessage."""
    if isinstance(m, dict):
        return m.get("tool_calls") or []
    return getattr(m, "tool_calls") or []


def trim_context(messages, max_len=14):
    """Trim conversation while preserving tool_call→tool pairs (by ID)."""
    if len(messages) <= max_len:
        return messages

    trimmed = messages[-max_len:]

    # Forward scan: collect valid tool_call_ids, drop orphaned results
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


def run_turn(messages, workdir, ui: TerminalUI | None = None):
    """One agent turn. Returns True if done."""
    ui = ui or TerminalUI()
    msg = chat(messages, TOOL_DEFS)
    messages.append(msg)

    for tc in (msg.tool_calls or []):
        name = tc.function.name
        args_raw = tc.function.arguments

        if name == "bash":
            cmd = json.loads(args_raw)["command"]
            if _is_dangerous(cmd) and not ui.confirm_dangerous_command(cmd, default="n"):
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": "[✗] User cancelled dangerous command",
                })
                ui.show_tool_cancelled(name, args_raw)
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
        return True
    return False


def run_loop(base_sys, working, workdir, max_turns=200, ui: TerminalUI | None = None):
    ui = ui or TerminalUI()
    # Agent 主循环：模型可以多轮思考、调用工具、读取工具结果，再继续下一轮。
    for turn in range(max_turns):
        # 每轮都用固定系统消息 + 当前对话上下文，组装成发给模型的完整 messages。
        messages = base_sys + working
        msg = chat(messages, TOOL_DEFS)

        # 先把模型本轮返回写入上下文；如果它请求工具调用，后面会继续追加工具结果。
        working.append(msg)

        for tc in (msg.tool_calls or []):
            name = tc.function.name
            args_raw = tc.function.arguments

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
            return

        # 控制上下文长度，避免长时间 REPL 或多轮工具调用导致 messages 过大。
        working = trim_context(working)

    ui.show_warning("Max turns reached. Stopping.")


def repl_completions(workdir: str, loaded_skills: list[str]) -> list[str]:
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
    commands.extend(f"/skill {name}" for name in skills)
    commands.extend(f"/{name}" for name in skills)
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
    if inp in ("exit", "quit", "/bye"):
        return ReplAction(handled=True, exit_requested=True)

    if inp == "clear":
        working.clear()
        ui.show_context_cleared()
        return ReplAction(handled=True)

    if inp.startswith("dir="):
        new_workdir = os.path.abspath(inp[4:])
        ui.show_workdir(new_workdir)
        return ReplAction(handled=True, workdir=new_workdir)

    if inp in ("/skill", "/skills"):
        show_skill_status(ui, workdir, loaded_skills)
        return ReplAction(handled=True)

    if inp.startswith("/skill "):
        name = inp[7:].strip()
        try:
            info, _ = load_skill(workdir, name)
            if name not in loaded_skills:
                loaded_skills.append(name)
            ui.show_loaded_skill(name, info.selected_source)
        except FileNotFoundError as e:
            ui.show_skill_error(e)
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
        else:
            ui.show_skill_not_loaded(name)
        return ReplAction(handled=True)

    if inp.startswith("/"):
        command = inp[1:].split(maxsplit=1)[0]
        try:
            invocation = parse_skill_invocation(workdir, inp)
        except FileNotFoundError:
            ui.show_unknown_command_or_skill(command)
            return ReplAction(handled=True)
        if invocation is None:
            ui.show_unknown_command_or_skill(command)
            return ReplAction(handled=True)
        name = invocation.info.name
        if name not in loaded_skills:
            loaded_skills.append(name)
            ui.show_loaded_skill(name, invocation.info.selected_source)
        else:
            ui.show_skill_already_loaded(name, invocation.info.selected_source)
        return ReplAction(handled=True, prompt=invocation.prompt or None)

    return ReplAction(handled=False)


# ═══════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════

def main():
    import argparse

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
            if name not in loaded_skills:
                loaded_skills.append(name)
            ui.show_loaded_skill(name, info.selected_source, indent="   ")
        except FileNotFoundError as e:
            ui.show_skill_error(e)
            raise SystemExit(2) from e

    if args.input:
        # 单次执行模式：命令行里带了 prompt，跑完一轮 run_loop 后直接退出。
        working = []
        working.append({"role": "user", "content": " ".join(args.input)})
        base_sys = build_messages(loaded_skills, workdir)
        run_loop(base_sys, working, workdir, max_turns=args.max_turns, ui=ui)
    else:
        # REPL 模式：没有一次性 prompt，就进入交互循环，持续保留 working 上下文。
        avail = describe_skills(workdir)
        ui.show_repl_help(skill_count=len(avail))
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
            working.append({"role": "user", "content": inp})
            base_sys = build_messages(loaded_skills, workdir)
            run_loop(base_sys, working, workdir, max_turns=args.max_turns, ui=ui)
            working = trim_context(working)


if __name__ == "__main__":
    main()
