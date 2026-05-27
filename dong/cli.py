"""dong CLI：带结构化工具 I/O 和 skill 加载能力的最小编码代理。"""

import json
import logging
import os
import shlex
import signal
import sys
import time
from contextlib import nullcontext
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from queue import Empty, Queue
from threading import Event, Thread

from rich.text import Text

from dong.context_compaction import (
    CONTEXT_SUMMARY_PREFIX,
    DEFAULT_CONTEXT_MAX_MESSAGES,
    MIN_TAIL_MESSAGES,
    compact_context,
    message_reasoning_content,
    trim_context,
)
from dong.contract import (
    CONTRACT_VERSION,
    ContractController,
    ContractEvidence,
    ContractMode,
    ContractSignature,
    ContractSignal,
    ScorerResult,
    VERIFY_COMMAND_KEYWORDS,
    apply_score,
    build_rule_floor,
    ensure_best_practices,
    load_scoreboard,
    pressure_summary,
    scorer_instructions,
    scorer_user_payload,
    sign_evidence,
    update_contract_artifact_scorer_result,
    validate_scorer_result,
    verify_signature,
    write_contract_artifact,
)
from dong.llm import chat, get_model_name
from dong.log_viewer import LogFilter, stream_logs
from dong.logging_config import (
    configure_logging,
    get_logger,
    log_event,
    payload_logging_enabled,
    preview_payload,
)
from dong.mcp import McpConfigFile, McpManager, config_path, configured_server_names
from dong.ocr import (
    OcrError,
    build_ocr_prompt,
    expand_image_markers_to_ocr_prompt,
    extract_text_from_image,
)
from dong.session import Session, SessionError, SessionStore
from dong.skills import (
    SKILLS_RELPATH,
    _codex_skills_dir,
    _skill_entry_names,
    build_skill_messages,
    describe_loaded_skills,
    describe_skills,
    list_skills,
    load_skill,
    parse_skill_invocation,
    resolve_skill,
)
from dong.skills import (
    SkillInfo as SkillInfo,
)
from dong.skills import (
    SkillInvocation as SkillInvocation,
)
from dong.skills import (
    SkillSource as SkillSource,
)
from dong.skills import (
    print_skill_status as print_skill_status,
)
from dong.tool import ToolResult
from dong.tools import TOOL_DEFS, _is_dangerous, execute
from dong.ui import TerminalUI

LOGGER = get_logger(__name__)
MAX_MODEL_LOADED_SKILLS_PER_TURN = 5
# 兼容旧的 dong.cli.trim_context 导入路径；新实现权威入口在 context_compaction。
__all__ = [
    "CONTEXT_SUMMARY_PREFIX",
    "DEFAULT_CONTEXT_MAX_MESSAGES",
    "trim_context",
]

DEFAULT_AGENT_DEFINE_FILENAME = "default_agent_define.md"
DONG_RULE_CANDIDATES = ["DONG.md", ".dong/DONG.md"]


def _is_dong_project_root(path: str) -> bool:
    """判断目录是否像 dong 仓库根目录，用于默认工作目录归一。"""
    return (
        os.path.isfile(os.path.join(path, "pyproject.toml"))
        and os.path.isdir(os.path.join(path, "dong"))
        and os.path.isfile(os.path.join(path, "dong", "__init__.py"))
    )


def _resolve_workdir(raw_dir: str, *, explicit: bool) -> str:
    """解析 CLI 工作目录；默认从包目录启动时回到仓库根，显式 -d 不改写。"""
    workdir = os.path.abspath(raw_dir)
    if explicit:
        return workdir

    parent = os.path.dirname(workdir)
    if (
        os.path.basename(workdir) == "dong"
        and os.path.isfile(os.path.join(workdir, "__init__.py"))
        and _is_dong_project_root(parent)
    ):
        return parent
    return workdir


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
class ReplAction:
    """REPL 命令处理结果，用于驱动退出、切目录或继续执行 prompt。"""

    handled: bool
    exit_requested: bool = False
    workdir: str | None = None
    prompt: str | None = None
    session: Session | None = None


@dataclass(frozen=True)
class AgentPrompt:
    """一次 agent 请求的固定提示词；instructions 是发给 LLM 的系统提示词。"""

    instructions: str
    context_messages: list


@dataclass(frozen=True)
class SessionListItem:
    """用于 `/sessions` 展示的 session 摘要和内容预览。"""

    session_id: str
    path: str
    updated_at_ms: int
    message_count: int
    model: str | None
    prompt_preview: str
    assistant_preview: str
    resume_command: str
    current: bool = False


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
#  Skill 状态展示与 prompt 组装
# ═══════════════════════════════════════


def show_skill_status(ui: TerminalUI, workdir: str, loaded_skills: list[str]) -> None:
    """通过 UI 适配层展示 skill 可用/已加载状态。"""
    avail = describe_skills(workdir)
    if avail:
        ui.err_console.print("  Available:")
        for item in avail:
            ui.err_console.print(f"    {item}")
    else:
        ui.err_console.print(
            f"  (no skills in {SKILLS_RELPATH}/ or {_codex_skills_dir()}/)"
        )
    if loaded_skills:
        ui.err_console.print("  Loaded:")
        for item in describe_loaded_skills(workdir, loaded_skills):
            ui.err_console.print(f"    {item}")
    else:
        ui.err_console.print("  (no skills loaded)")


def _session_resume_command(workdir: str, session_id: str) -> str:
    """生成可复制的恢复当前 session 命令。"""
    return f"dong -d {shlex.quote(workdir)} --resume {shlex.quote(session_id)}"


def _preview_text(value, *, limit: int = 96) -> str:
    """把消息内容压成单行预览，避免 session 列表占满屏幕。"""
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        text = " ".join(parts)
    else:
        text = str(value or "")
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _session_list_items(
    workdir: str,
    session: Session | None,
) -> list[SessionListItem]:
    """读取当前工作区 session 摘要，并补充最近对话内容预览。"""
    store = SessionStore(workdir)
    current_session_id = session.session_id if session is not None else None
    items: list[SessionListItem] = []
    for summary in store.list_summaries():
        prompt_preview = ""
        assistant_preview = ""
        try:
            loaded = Session.load_from_path(summary.path)
        except SessionError:
            loaded = None
        if loaded is not None:
            for prompt in reversed(loaded.prompt_history):
                prompt_preview = _preview_text(prompt.get("text"))
                if prompt_preview:
                    break
            for message in reversed(loaded.messages):
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                if not prompt_preview and role == "user":
                    prompt_preview = _preview_text(message.get("content"))
                if not assistant_preview and role == "assistant":
                    assistant_preview = _preview_text(message.get("content"))
                if prompt_preview and assistant_preview:
                    break

        items.append(
            SessionListItem(
                session_id=summary.session_id,
                path=str(summary.path),
                updated_at_ms=summary.updated_at_ms,
                message_count=summary.message_count,
                model=summary.model,
                prompt_preview=prompt_preview or "(no user prompt)",
                assistant_preview=assistant_preview or "(no assistant reply)",
                resume_command=_session_resume_command(workdir, summary.session_id),
                current=summary.session_id == current_session_id,
            )
        )
    return items


def _attach_session_to_working(loaded: Session, working: list) -> Session:
    """把已加载 session 接到当前 REPL working 列表，保持后续写盘同步。"""
    working[:] = loaded.messages
    loaded.messages = working
    return loaded


def _show_session_list(
    ui: TerminalUI,
    workdir: str,
    session: Session | None,
    working: list,
) -> Session | None:
    """展示 session 列表；TUI 下可选择并切换当前 session。"""
    items = _session_list_items(workdir, session)
    current_session_id = session.session_id if session is not None else None
    selector = getattr(ui, "select_session", None)
    if callable(selector):
        selected_id = selector(items, current_session_id=current_session_id)
        if not selected_id:
            return None
        loaded = SessionStore(workdir).load(selected_id)
        loaded = _attach_session_to_working(loaded, working)
        ui.show_session_restored(
            loaded.session_id,
            _session_resume_command(workdir, loaded.session_id),
            loaded.messages,
        )
        return loaded
    ui.show_session_summaries(items, current_session_id=current_session_id)
    return None


def _show_resume_command(
    ui: TerminalUI,
    workdir: str,
    session: Session | None,
) -> None:
    """在退出交互会话前打印完整恢复命令。"""
    if session is None:
        return
    ui.show_session_resume_command(
        _session_resume_command(workdir, session.session_id)
    )


def build_messages(loaded_skills: list[str], workdir: str) -> list[dict[str, str]]:
    """构建动态系统消息；默认系统提示词由 instructions 单独承载。"""
    messages = []
    project_rules = load_dong_md(workdir)
    if project_rules:
        messages.append(project_rules)
    messages.extend(build_skill_messages(loaded_skills, workdir))
    return messages


def build_agent_prompt(loaded_skills: list[str], workdir: str) -> AgentPrompt:
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
    return input(prompt + suffix).strip().lower() in (
        "y",
        "yes",
        "" if default == "y" else "",
    )


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


def _tool_timeout_seconds(
    name: str, args_raw: str, mcp_manager: McpManager
) -> float | None:
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


def _model_skill_from_args(args_raw: str) -> tuple[str | None, ToolResult | None]:
    """从 skill_load 参数中解析规范 skill 名，供本轮临时注入逻辑使用。"""
    try:
        args = json.loads(args_raw)
    except Exception as exc:
        return None, _invalid_tool_input_result("skill_load", exc)
    if not isinstance(args, dict):
        return None, _invalid_tool_input_result(
            "skill_load", "arguments must be a JSON object"
        )
    skill_name = args.get("skill")
    if not isinstance(skill_name, str) or not skill_name.strip():
        return None, _invalid_tool_input_result("skill_load", "skill must be a string")
    return skill_name.strip(), None


def _instructions_for_turn_skills(
    base_instructions: str,
    *,
    workdir: str,
    turn_skills: list[str],
) -> str:
    """把模型本轮通过 skill_load 选择的临时 skill 追加到下一次请求 instructions。"""
    if not turn_skills:
        return base_instructions
    parts = [base_instructions]
    for message in build_skill_messages(turn_skills, workdir):
        content = (message.get("content") or "").strip()
        if content:
            parts.append(content)
    return "\n\n".join(parts)


def _latest_contract_lesson(session: Session | None) -> str:
    """从 session 事件中取最近一条契约教训，供下一轮压力摘要注入。"""
    if session is None:
        return ""
    for event in reversed(session.events):
        if event.get("type") != "contract_lesson":
            continue
        lesson = event.get("lesson_for_session")
        if isinstance(lesson, str):
            return lesson
    return ""


def _contract_trigger_reasons(controller: ContractController) -> list[str]:
    """把契约触发原因转成稳定日志字段，方便后续排查压力来源。"""
    return sorted(reason.value for reason in controller.trigger_reasons)


def _instructions_for_contract_pressure(
    base_instructions: str,
    *,
    contract_controller: ContractController,
    workdir: str,
    session: Session | None,
) -> str:
    """按当前契约状态追加压力摘要；未激活时保持原 instructions 不变。"""
    scoreboard = load_scoreboard(workdir)
    lesson_for_session = _latest_contract_lesson(session)
    summary = pressure_summary(
        contract_controller,
        average_score=scoreboard.average_score,
        pressure_level=scoreboard.pressure_level,
        lesson_for_session=lesson_for_session,
    )
    if not summary and lesson_for_session:
        # session 教训来自第三方 scorer，恢复同一 session 时也应注入下一轮上下文。
        log_event(
            LOGGER,
            logging.INFO,
            "contract_lesson_injected",
        )
        return f"{base_instructions}\n\n[Contract Lesson | 契约教训] {lesson_for_session}"
    if not summary:
        return base_instructions
    log_event(
        LOGGER,
        logging.INFO,
        "contract_pressure_injected",
        reasons=_contract_trigger_reasons(contract_controller),
    )
    return f"{base_instructions}\n\n{summary}"


def _record_contract_compaction(
    contract_controller: ContractController,
    compaction,
) -> None:
    """压缩上下文后记录契约信号，下一轮可据此注入交付压力。"""
    if not compaction.compacted:
        return
    _record_contract_signal(
        contract_controller,
        ContractSignal.compaction(compaction.summary_ref or ""),
    )


def _record_contract_signal(
    contract_controller: ContractController,
    signal: ContractSignal,
) -> None:
    """记录契约信号，并在激活原因新增时留下统一审计日志。"""

    was_active = contract_controller.is_active()
    before_reasons = set(contract_controller.trigger_reasons)
    contract_controller.record_signal(signal)
    reasons_changed = before_reasons != contract_controller.trigger_reasons
    if contract_controller.is_active() and (not was_active or reasons_changed):
        log_event(
            LOGGER,
            logging.INFO,
            "contract_triggered",
            reasons=_contract_trigger_reasons(contract_controller),
        )


def _contract_session_id(session: Session | None) -> str:
    """生成契约证据使用的 session id；无持久 session 时用临时时间戳。"""
    if session is not None and session.session_id:
        return session.session_id
    return f"session-{int(time.time() * 1000)}"


def _last_user_prompt(messages: list) -> str:
    """从当前上下文中取最近一条用户输入，作为本轮契约目标。"""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
    return ""


def _contract_tool_args(detail: str) -> dict:
    """解析工具参数 JSON；解析失败时返回空 dict，避免影响最终答复。"""
    try:
        payload = json.loads(detail)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _looks_like_contract_verification(command: str) -> bool:
    """用保守关键词识别契约证据里的验证命令。"""
    command_lower = command.lower()
    return any(keyword in command_lower for keyword in VERIFY_COMMAND_KEYWORDS)


def _build_contract_evidence(
    session: Session | None,
    controller: ContractController,
    user_objective: str,
    final_answer: str,
) -> ContractEvidence:
    """从控制器轨迹构造最终答复后的契约证据包。"""
    tool_summary: list[dict] = []
    file_changes: list[dict] = []
    verification_evidence: list[dict] = []
    saw_file_change = False

    for contract_signal in controller.tool_calls:
        args = _contract_tool_args(contract_signal.detail)
        item = {
            "kind": contract_signal.kind,
            "name": contract_signal.name,
            "detail_chars": len(contract_signal.detail),
        }
        if contract_signal.kind == "tool_result":
            item["success"] = contract_signal.success
            item["error"] = contract_signal.error
            item["tool_call_id"] = contract_signal.tool_call_id
        tool_summary.append(item)
        if contract_signal.kind == "tool_call" and contract_signal.name in {"write", "edit"}:
            saw_file_change = True
            file_changes.append({
                "tool": contract_signal.name,
                "filepath": args.get("filepath", ""),
            })
        if (
            contract_signal.kind == "tool_call"
            and saw_file_change
            and contract_signal.name in {"read", "grep"}
        ):
            verification_evidence.append({
                "tool": contract_signal.name,
                "filepath": args.get("filepath") or args.get("path") or "",
                "pattern": args.get("pattern", ""),
            })
        if contract_signal.kind == "tool_call" and contract_signal.name == "bash":
            command = str(args.get("command") or args.get("cmd") or "")
            if _looks_like_contract_verification(command):
                verification_evidence.append({
                    "tool": contract_signal.name,
                    "command": command,
                })

    unverified_items: list[str] = []
    if file_changes and not verification_evidence:
        unverified_items.append("代码修改后未观察到验证命令")

    return ContractEvidence(
        contract_version=CONTRACT_VERSION,
        session_id=_contract_session_id(session),
        trigger_reasons=_contract_trigger_reasons(controller),
        user_objective=user_objective,
        tool_summary=tool_summary,
        file_changes=file_changes,
        verification_evidence=verification_evidence,
        final_answer=final_answer,
        known_risks=[],
        unverified_items=unverified_items,
    )


def _run_contract_scorer(
    workdir: str,
    evidence: ContractEvidence,
    signature: ContractSignature,
) -> ScorerResult:
    """调用第三方 scorer LLM，按规则底座校验并返回本轮评分。"""

    best_practices_path = ensure_best_practices(workdir)
    best_practices = best_practices_path.read_text(encoding="utf-8")
    scoreboard = load_scoreboard(workdir)
    signature_valid = verify_signature(evidence, signature)
    rule_floor = build_rule_floor(evidence, signature_valid=signature_valid)
    log_event(
        LOGGER,
        logging.INFO,
        "contract_rule_floor_created",
        ceiling=rule_floor.base_score_ceiling,
        signature_valid=signature_valid,
    )
    scorer_message = chat(
        [
            {
                "role": "user",
                "content": scorer_user_payload(
                    best_practices=best_practices,
                    evidence=evidence,
                    rule_floor=rule_floor,
                    scoreboard=scoreboard,
                ),
            }
        ],
        [],
        instructions=scorer_instructions(),
    )
    raw = json.loads(scorer_message.content or "{}")
    if not isinstance(raw, dict):
        raise ValueError("contract scorer output must be a JSON object")
    return validate_scorer_result(raw, rule_floor)


def _record_model_loaded_skill(
    *,
    args_raw: str,
    workdir: str,
    turn_skills: list[str],
) -> ToolResult | None:
    """处理 skill_load 的本轮状态更新；超过 5 个时拒绝继续注入。"""
    requested, invalid_result = _model_skill_from_args(args_raw)
    if invalid_result is not None:
        return invalid_result
    assert requested is not None
    try:
        info = resolve_skill(workdir, requested)
    except FileNotFoundError as exc:
        return ToolResult(success=False, error=f"File not found: {exc}")
    if info.name in turn_skills:
        return ToolResult(
            success=True,
            summary=f"Skill already queued for this turn: {info.name}",
        )
    if len(turn_skills) >= MAX_MODEL_LOADED_SKILLS_PER_TURN:
        return ToolResult(
            success=False,
            error=(
                "Model-loaded skill limit reached for this user turn "
                f"({MAX_MODEL_LOADED_SKILLS_PER_TURN})."
            ),
        )
    turn_skills.append(info.name)
    log_event(
        LOGGER,
        logging.INFO,
        "model_skill_loaded",
        skill=info.name,
        loaded_count=len(turn_skills),
        max_skills=MAX_MODEL_LOADED_SKILLS_PER_TURN,
    )
    return None


def _bash_command_from_args(args_raw: str) -> tuple[str | None, ToolResult | None]:
    """安全读取 bash command，供危险命令确认使用。"""
    try:
        args = json.loads(args_raw)
    except Exception as exc:
        return None, _invalid_tool_input_result("bash", exc)
    if not isinstance(args, dict):
        return None, _invalid_tool_input_result(
            "bash", "arguments must be a JSON object"
        )
    command = args.get("command")
    if not isinstance(command, str):
        return None, _invalid_tool_input_result("bash", "command must be a string")
    return command, None


def _append_context_message(
    working: list,
    message,
    *,
    session: Session | None = None,
) -> None:
    """把消息追加到运行上下文；有 session 时同步持久化并让失败回滚。"""
    if session is None:
        working.append(message)
        return
    if working is session.messages:
        session.append_message(message)
        return
    working.append(message)
    try:
        session.append_message(message)
    except Exception:
        working.pop()
        raise


def _replace_context_messages(
    working: list,
    messages: list,
    *,
    session: Session | None = None,
    compaction: dict | None = None,
) -> None:
    """替换运行上下文；有 session 时用 snapshot 固化压缩或清空后的状态。"""
    if session is None:
        working[:] = messages
        return
    session.replace_messages(messages, compaction=compaction)
    if working is not session.messages:
        working[:] = session.messages


def _compaction_record(compaction) -> dict | None:
    """把上下文压缩结果转成 session metadata；未压缩时不生成记录。"""
    if not compaction.compacted:
        return None
    return {
        "summary_ref": compaction.summary_ref,
        "removed_messages": compaction.removed_messages,
        "preserved_messages": compaction.preserved_messages,
        "estimated_tokens_before": compaction.estimated_tokens_before,
        "estimated_tokens_after": compaction.estimated_tokens_after,
        "budget_limit": compaction.budget.limit,
        "model": compaction.budget.model,
    }


def _apply_compaction_result(
    working: list,
    compaction,
    *,
    session: Session | None = None,
) -> None:
    """应用 compact_context 返回值，并在 session 中保存压缩元信息。"""
    if compaction.messages is working and not compaction.compacted:
        return
    _replace_context_messages(
        working,
        compaction.messages,
        session=session,
        compaction=_compaction_record(compaction),
    )


def _show_context_usage(ui: TerminalUI, compaction) -> None:
    """把本轮 context 预算使用情况交给 UI 展示；普通终端默认忽略。"""
    ui.show_context_usage(
        estimated_tokens=compaction.estimated_tokens_after,
        budget_limit=compaction.budget.limit,
        context_window_tokens=compaction.budget.context_window_tokens,
        compacted=compaction.compacted,
    )


def _append_tool_result(
    messages: list,
    tool_call_id: str,
    result: ToolResult,
    *,
    session: Session | None = None,
) -> None:
    """把工具结果按 provider 需要的 tool message 形状追加进上下文。"""
    _append_context_message(
        messages,
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result.to_message()["content"],
        },
        session=session,
    )


def _tool_call_log_payload(tool_call) -> dict:
    """把模型请求的工具调用转成可检索日志字段。"""
    arguments = tool_call.function.arguments
    payload = {
        "id": tool_call.id,
        "name": tool_call.function.name,
        "arguments_chars": len(arguments),
    }
    if payload_logging_enabled():
        payload["arguments_preview"] = preview_payload(arguments)
    return payload


def _log_ai_message(message, *, turn: int | None = None) -> None:
    """记录 AI 本轮完整思考、回复和工具调用，便于事后排查。"""
    content = getattr(message, "content", None) or ""
    reasoning = message_reasoning_content(message)
    tool_calls = [
        _tool_call_log_payload(tool_call)
        for tool_call in (getattr(message, "tool_calls", None) or [])
    ]
    usage = getattr(message, "usage", None)
    fields = {
        "content_chars": len(content),
        "reasoning_chars": len(reasoning),
        "tool_calls": tool_calls,
        "tool_call_count": len(tool_calls),
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
        "total_tokens": getattr(usage, "total_tokens", 0),
    }
    if turn is not None:
        fields["turn"] = turn
    if payload_logging_enabled():
        fields["content_preview"] = preview_payload(content)
        fields["reasoning_preview"] = preview_payload(reasoning)
    log_event(LOGGER, logging.INFO, "ai_message_received", **fields)


def _log_ai_tool_call(tool_call, *, turn: int | None = None) -> None:
    """记录 AI 选择的单个工具和原始参数。"""
    payload = _tool_call_log_payload(tool_call)
    if turn is not None:
        payload["turn"] = turn
    log_event(LOGGER, logging.INFO, "ai_tool_call_requested", **payload)


def _log_ai_tool_result(
    tool: str,
    tool_call_id: str,
    result: ToolResult,
    *,
    turn: int | None = None,
) -> None:
    """记录工具执行后回传给 AI 的结果正文。"""
    content = result.to_message()["content"]
    fields = {
        "tool": tool,
        "tool_call_id": tool_call_id,
        "success": result.success,
        "summary": result.summary,
        "detail_chars": len(result.detail or ""),
        "error_chars": len(result.error or ""),
        "content_chars": len(content),
    }
    if turn is not None:
        fields["turn"] = turn
    if payload_logging_enabled():
        fields["detail_preview"] = preview_payload(result.detail or "")
        fields["error_preview"] = preview_payload(result.error or "")
        fields["content_preview"] = preview_payload(content)
    log_event(
        LOGGER,
        logging.INFO if result.success else logging.WARNING,
        "ai_tool_result_received",
        **fields,
    )


def _show_task_interrupted(
    ui: TerminalUI, *, phase: str, tool: str | None = None
) -> None:
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
    reasoning_stream_factory = getattr(ui, "stream_reasoning_message", None)

    try:
        working_status.__enter__()
        working_entered = True
        reasoning_stream = (
            reasoning_stream_factory()
            if callable(reasoning_stream_factory)
            else nullcontext(lambda _delta: None)
        )
        with (
            ui.stream_assistant_message() as write_delta,
            reasoning_stream as write_reasoning_delta,
        ):

            def on_text_delta(delta: str) -> None:
                nonlocal working_active
                if not delta:
                    return
                if working_active and working_entered:
                    working_status.__exit__(None, None, None)
                    working_active = False
                write_delta(delta)

            def on_reasoning_delta(delta: str) -> None:
                if not delta:
                    return
                write_reasoning_delta(delta)

            message = chat(
                messages,
                tool_defs,
                instructions=instructions,
                on_text_delta=on_text_delta,
                on_reasoning_delta=on_reasoning_delta,
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
    mcp_manager = (
        _start_mcp_manager(workdir, ui) if enable_mcp else _empty_mcp_manager(workdir)
    )
    turn_skills: list[str] = []
    try:
        tool_defs = TOOL_DEFS + mcp_manager.tool_definitions
        turn_instructions = _instructions_for_turn_skills(
            agent_prompt.instructions,
            workdir=workdir,
            turn_skills=turn_skills,
        )
        compaction = compact_context(
            agent_prompt.context_messages,
            workdir=workdir,
            model=get_model_name(),
            instructions=turn_instructions,
            tools=tool_defs,
            reason="preflight",
        )
        request_messages = compaction.messages
        _show_context_usage(ui, compaction)
        try:
            msg = _chat_with_streaming_ui(
                request_messages,
                tool_defs,
                turn_instructions,
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
        _log_ai_message(msg)
        reasoning = message_reasoning_content(msg)
        if reasoning:
            ui.show_reasoning_message(reasoning)

        for tc in msg.tool_calls or []:
            name = tc.function.name
            args_raw = tc.function.arguments
            _log_ai_tool_call(tc)
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
                if _is_dangerous(cmd) and not ui.confirm_dangerous_command(
                    cmd, default="n"
                ):
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": "[✗] User cancelled dangerous command",
                        }
                    )
                    ui.show_tool_cancelled(name, args_raw)
                    log_event(LOGGER, logging.WARNING, "tool_call_cancelled", tool=name)
                    continue

            try:
                with ui.show_working(
                    f"正在执行工具：{name}",
                    timeout_seconds=_tool_timeout_seconds(name, args_raw, mcp_manager),
                ):
                    result = _execute_registered_tool(
                        name, args_raw, workdir, mcp_manager
                    )
            except KeyboardInterrupt:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": "[✗] User cancelled current task",
                    }
                )
                _show_task_interrupted(ui, phase="tool", tool=name)
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "run_turn_interrupted",
                    phase="tool",
                    tool=name,
                )
                return False
            if name == "skill_load":
                runtime_result = _record_model_loaded_skill(
                    args_raw=args_raw,
                    workdir=workdir,
                    turn_skills=turn_skills,
                )
                if runtime_result is not None:
                    result = runtime_result
            ui.show_tool_result(name, args_raw, result)
            _log_ai_tool_result(name, tc.id, result)
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
    session: Session | None = None,
    contract_controller: ContractController | None = None,
):
    """执行多轮 agent 循环，直到模型给出最终文本或达到轮数上限。"""
    ui = ui or TerminalUI()
    contract_controller = contract_controller or ContractController(workdir=workdir)
    agent_prompt = (
        base_sys
        if isinstance(base_sys, AgentPrompt)
        else _agent_prompt_from_messages(base_sys)
    )
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
    mcp_manager = (
        _start_mcp_manager(workdir, ui) if enable_mcp else _empty_mcp_manager(workdir)
    )
    turn_skills: list[str] = []
    try:
        tool_defs = TOOL_DEFS + mcp_manager.tool_definitions
        # Agent 主循环：模型可以多轮思考、调用工具、读取工具结果，再继续下一轮。
        for turn in range(max_turns):
            # 每轮都用固定 input 上下文 + 当前对话上下文，系统提示词单独走 instructions。
            skill_instructions = _instructions_for_turn_skills(
                agent_prompt.instructions,
                workdir=workdir,
                turn_skills=turn_skills,
            )
            turn_instructions = _instructions_for_contract_pressure(
                skill_instructions,
                contract_controller=contract_controller,
                workdir=workdir,
                session=session,
            )
            compaction = compact_context(
                working,
                workdir=workdir,
                model=get_model_name(),
                instructions=turn_instructions,
                tools=tool_defs,
                fixed_messages=agent_prompt.context_messages,
                reason="preflight",
            )
            _apply_compaction_result(working, compaction, session=session)
            _record_contract_compaction(contract_controller, compaction)
            turn_instructions = _instructions_for_contract_pressure(
                skill_instructions,
                contract_controller=contract_controller,
                workdir=workdir,
                session=session,
            )
            _show_context_usage(ui, compaction)
            messages = agent_prompt.context_messages + working
            log_event(
                LOGGER,
                logging.DEBUG,
                "run_loop_turn_started",
                turn=turn + 1,
                messages=len(messages),
                estimated_tokens=compaction.estimated_tokens_after,
                budget_limit=compaction.budget.limit,
            )
            try:
                msg = _chat_with_streaming_ui(
                    messages,
                    tool_defs,
                    turn_instructions,
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
            _append_context_message(working, msg, session=session)
            _log_ai_message(msg, turn=turn + 1)
            reasoning = message_reasoning_content(msg)
            if reasoning:
                ui.show_reasoning_message(reasoning)

            for tc in msg.tool_calls or []:
                name = tc.function.name
                args_raw = tc.function.arguments
                _log_ai_tool_call(tc, turn=turn + 1)
                _record_contract_signal(
                    contract_controller,
                    ContractSignal.tool_call(name, args_raw)
                )
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
                        _record_contract_signal(
                            contract_controller,
                            ContractSignal.tool_result(
                                name,
                                success=invalid_result.success,
                                error=invalid_result.error,
                                tool_call_id=tc.id,
                            ),
                        )
                        ui.show_tool_result(name, args_raw, invalid_result)
                        _append_tool_result(
                            working,
                            tc.id,
                            invalid_result,
                            session=session,
                        )
                        log_event(
                            LOGGER,
                            logging.WARNING,
                            "tool_call_invalid_input",
                            tool=name,
                            error=invalid_result.error,
                            turn=turn + 1,
                        )
                        continue
                    if _is_dangerous(cmd) and not ui.confirm_dangerous_command(
                        cmd, default="n"
                    ):
                        # 用户拒绝危险命令时，也要把拒绝结果写回上下文，让模型知道发生了什么。
                        _record_contract_signal(
                            contract_controller,
                            ContractSignal.tool_result(
                                name,
                                success=False,
                                error="User cancelled dangerous command",
                                tool_call_id=tc.id,
                            ),
                        )
                        _append_context_message(
                            working,
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": "[✗] User cancelled dangerous command",
                            },
                            session=session,
                        )
                        ui.show_tool_cancelled(name, args_raw)
                        log_event(
                            LOGGER, logging.WARNING, "tool_call_cancelled", tool=name
                        )
                        continue

                # 统一执行内置工具或 MCP 工具，并把结果追加回上下文。
                try:
                    with ui.show_working(
                        f"正在执行工具：{name}",
                        timeout_seconds=_tool_timeout_seconds(
                            name, args_raw, mcp_manager
                        ),
                    ):
                        result = _execute_registered_tool(
                            name, args_raw, workdir, mcp_manager
                        )
                except KeyboardInterrupt:
                    _append_context_message(
                        working,
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": "[✗] User cancelled current task",
                        },
                        session=session,
                    )
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
                if name == "skill_load":
                    runtime_result = _record_model_loaded_skill(
                        args_raw=args_raw,
                        workdir=workdir,
                        turn_skills=turn_skills,
                    )
                    if runtime_result is not None:
                        result = runtime_result
                _record_contract_signal(
                    contract_controller,
                    ContractSignal.tool_result(
                        name,
                        success=result.success,
                        error=result.error,
                        tool_call_id=tc.id,
                    ),
                )
                ui.show_tool_result(name, args_raw, result)
                _log_ai_tool_result(name, tc.id, result, turn=turn + 1)

                # 工具结果必须作为 tool message 追加回上下文，模型下一轮才能基于结果继续判断。
                _append_tool_result(working, tc.id, result, session=session)

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
                if contract_controller.is_active():
                    # 契约签名只在最终答复后执行，避免把未完成的工具循环写成证据包。
                    evidence = _build_contract_evidence(
                        session,
                        contract_controller,
                        _last_user_prompt(working),
                        msg.content,
                    )
                    log_event(
                        LOGGER,
                        logging.INFO,
                        "contract_evidence_created",
                        session_id=evidence.session_id,
                        file_change_count=len(evidence.file_changes),
                        verification_count=len(evidence.verification_evidence),
                        unverified_count=len(evidence.unverified_items),
                    )
                    signature_started_at = time.perf_counter()
                    log_event(
                        LOGGER,
                        logging.INFO,
                        "contract_signature_started",
                        session_id=evidence.session_id,
                        difficulty=1,
                    )
                    try:
                        signature: ContractSignature = sign_evidence(
                            evidence,
                            difficulty=1,
                        )
                    except Exception as exc:
                        log_event(
                            LOGGER,
                            logging.WARNING,
                            "contract_signature_failed",
                            session_id=evidence.session_id,
                            error=str(exc),
                        )
                        return
                    artifact_path = write_contract_artifact(
                        workdir,
                        evidence,
                        signature,
                    )
                    if session is not None:
                        session.record_event(
                            "contract_signed",
                            {
                                "artifact_path": str(artifact_path),
                                "signature_hash": signature.signature_hash,
                                "difficulty": signature.difficulty,
                            },
                        )
                    log_event(
                        LOGGER,
                        logging.INFO,
                        "contract_signature_finished",
                        artifact_path=str(artifact_path),
                        elapsed_ms=int((time.perf_counter() - signature_started_at) * 1000),
                    )
                    try:
                        # scorer 失败不能影响主交付完成，只记录失败事件供排查。
                        log_event(
                            LOGGER,
                            logging.INFO,
                            "contract_scorer_started",
                            session_id=evidence.session_id,
                        )
                        scorer_result = _run_contract_scorer(
                            workdir,
                            evidence,
                            signature,
                        )
                        update_contract_artifact_scorer_result(
                            artifact_path,
                            scorer_result,
                        )
                        log_event(
                            LOGGER,
                            logging.INFO,
                            "contract_scorer_finished",
                            session_id=evidence.session_id,
                            score=scorer_result.score,
                        )
                        scoreboard = apply_score(
                            workdir,
                            load_scoreboard(workdir),
                            evidence.session_id,
                            scorer_result,
                        )
                        if session is not None:
                            session.record_event(
                                "contract_scored",
                                {
                                    "score": scorer_result.score,
                                    "deductions": scorer_result.deductions,
                                    "risk_flags": scorer_result.risk_flags,
                                },
                            )
                            session.record_event(
                                "contract_lesson",
                                {
                                    "lesson_for_session": scorer_result.lesson_for_session,
                                },
                            )
                        log_event(
                            LOGGER,
                            logging.INFO,
                            "contract_scoreboard_updated",
                            score=scorer_result.score,
                            pressure_level=scoreboard.pressure_level,
                        )
                    except Exception as exc:
                        log_event(
                            LOGGER,
                            logging.WARNING,
                            "contract_scorer_failed",
                            error=str(exc),
                        )
                return

            # 控制上下文长度，避免长时间 REPL 或多轮工具调用导致 messages 过大。
            compaction = compact_context(
                working,
                workdir=workdir,
                model=get_model_name(),
                instructions=_instructions_for_contract_pressure(
                    _instructions_for_turn_skills(
                        agent_prompt.instructions,
                        workdir=workdir,
                        turn_skills=turn_skills,
                    ),
                    contract_controller=contract_controller,
                    workdir=workdir,
                    session=session,
                ),
                tools=tool_defs,
                fixed_messages=agent_prompt.context_messages,
                reason="post_turn",
            )
            _apply_compaction_result(working, compaction, session=session)
            _record_contract_compaction(contract_controller, compaction)
            _show_context_usage(ui, compaction)
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
        "/compact",
        "dir=",
        "/skill",
        "/skills",
        "/sessions",
        "/contract",
        "/contract on",
        "/contract off",
        "/contract status",
        "/ocr",
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
    session: Session | None = None,
    contract_controller: ContractController | None = None,
) -> ReplAction:
    """处理 REPL 内置命令，普通用户输入会返回 handled=False。"""
    if inp in ("exit", "quit", "/bye"):
        _show_resume_command(ui, workdir, session)
        log_event(LOGGER, logging.INFO, "repl_exit_requested", command=inp)
        return ReplAction(handled=True, exit_requested=True)

    if inp == "clear":
        _replace_context_messages(working, [], session=session)
        ui.show_context_cleared()
        log_event(LOGGER, logging.INFO, "repl_context_cleared")
        return ReplAction(handled=True)

    if inp == "/compact":
        compaction = compact_context(
            working,
            workdir=workdir,
            model=get_model_name(),
            force=True,
            preserve_recent_messages=MIN_TAIL_MESSAGES,
            reason="manual",
        )
        _apply_compaction_result(working, compaction, session=session)
        _show_context_usage(ui, compaction)
        ui.show_context_compacted(
            compacted=compaction.compacted,
            removed_messages=compaction.removed_messages,
            preserved_messages=compaction.preserved_messages,
            summary_ref=compaction.summary_ref,
        )
        log_event(
            LOGGER,
            logging.INFO,
            "repl_context_compact_requested",
            compacted=compaction.compacted,
            removed_messages=compaction.removed_messages,
            preserved_messages=compaction.preserved_messages,
            summary_ref=compaction.summary_ref,
        )
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
        if contract_controller is not None:
            contract_controller.workdir = new_workdir
        ui.show_workdir(new_workdir)
        log_event(LOGGER, logging.INFO, "repl_workdir_changed", workdir=new_workdir)
        return ReplAction(handled=True, workdir=new_workdir)

    if inp == "/":
        ui.show_slash_commands()
        log_event(LOGGER, logging.INFO, "repl_slash_commands_shown")
        return ReplAction(handled=True)

    if inp in ("/skill", "/skills"):
        show_skill_status(ui, workdir, loaded_skills)
        log_event(LOGGER, logging.INFO, "repl_skill_status_shown")
        return ReplAction(handled=True)

    if inp == "/sessions":
        selected_session = _show_session_list(ui, workdir, session, working)
        log_event(LOGGER, logging.INFO, "repl_session_list_shown")
        return ReplAction(handled=True, session=selected_session)

    if inp == "/contract" or inp.startswith("/contract "):
        return _handle_contract_command(
            inp,
            workdir=workdir,
            ui=ui,
            contract_controller=contract_controller,
        )

    if inp == "/ocr" or inp.startswith("/ocr "):
        return _handle_ocr_command(inp, ui=ui)

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
        ui.show_skill_match(name, mode="slash", source=invocation.info.selected_source)
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
            prompt = inp[len(name) :].strip()
            try:
                info = resolve_skill(workdir, name)
                if info.name not in loaded_skills:
                    loaded_skills.append(info.name)
                ui.show_skill_match(info.name, mode="bare", source=info.selected_source)
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


def _show_contract_status(
    *,
    ui: TerminalUI,
    controller: ContractController,
) -> None:
    """汇总当前契约压力状态；评分表只读，用于命令行状态展示。"""
    scoreboard = load_scoreboard(controller.workdir)
    trigger_reasons = sorted(reason.value for reason in controller.trigger_reasons)
    presenter = getattr(ui, "show_contract_status", None)
    if callable(presenter):
        presenter(
            mode=controller.mode.value,
            active=controller.is_active(),
            pressure_level=scoreboard.pressure_level,
            average_score=scoreboard.average_score,
            trigger_reasons=trigger_reasons,
        )
        return

    # TUI 适配层不是 TerminalUI 子类时，退回到 err_console 的通用 print 接口。
    score = (
        "none"
        if scoreboard.average_score is None
        else f"{scoreboard.average_score:.1f}"
    )
    ui.err_console.print(f"contract mode: {controller.mode.value}")
    ui.err_console.print(f"active: {controller.is_active()}")
    ui.err_console.print(f"pressure level: {scoreboard.pressure_level}")
    ui.err_console.print(f"average score: {score}")
    if trigger_reasons:
        ui.err_console.print(f"trigger reasons: {', '.join(trigger_reasons)}")


def _handle_contract_command(
    inp: str,
    *,
    workdir: str,
    ui: TerminalUI,
    contract_controller: ContractController | None,
) -> ReplAction:
    """处理 `/contract` 命令；这里只控制模式，不做压力注入。"""
    controller = contract_controller or ContractController(workdir=workdir)
    parts = inp.split(maxsplit=1)
    subcommand = parts[1].strip().lower() if len(parts) > 1 else "status"

    if subcommand == "on":
        controller.set_mode(ContractMode.ON)
        _show_contract_status(ui=ui, controller=controller)
        log_event(LOGGER, logging.INFO, "contract_manual_on")
        return ReplAction(handled=True)

    if subcommand == "off":
        controller.set_mode(ContractMode.OFF)
        _show_contract_status(ui=ui, controller=controller)
        log_event(LOGGER, logging.INFO, "contract_manual_off")
        return ReplAction(handled=True)

    if subcommand == "status":
        _show_contract_status(ui=ui, controller=controller)
        log_event(LOGGER, logging.INFO, "contract_status_shown")
        return ReplAction(handled=True)

    ui.show_error("Usage: /contract [on|off|status]")
    log_event(LOGGER, logging.WARNING, "contract_command_invalid", command=subcommand)
    return ReplAction(handled=True)


def _handle_ocr_command(inp: str, *, ui: TerminalUI) -> ReplAction:
    """处理 `/ocr <image-path> [question]`，把图片转成文本 prompt。"""
    try:
        parts = shlex.split(inp)
    except ValueError as exc:
        ui.show_error(f"OCR 命令解析失败：{exc}")
        log_event(LOGGER, logging.WARNING, "repl_ocr_parse_failed", error=str(exc))
        return ReplAction(handled=True)

    if len(parts) < 2:
        ui.err_console.print(Text("  Usage: /ocr <image-path> [question]"))
        return ReplAction(handled=True)

    image_path = parts[1]
    question = " ".join(parts[2:])
    try:
        result = extract_text_from_image(image_path)
    except OcrError as exc:
        ui.show_error(str(exc))
        log_event(
            LOGGER,
            logging.WARNING,
            "repl_ocr_failed",
            image_path=image_path,
            error=str(exc),
        )
        return ReplAction(handled=True)

    prompt = build_ocr_prompt(result, question)
    log_event(
        LOGGER,
        logging.INFO,
        "repl_ocr_completed",
        image_path=result.image_path,
        text_chars=len(result.text),
        lines=len(result.lines),
    )
    return ReplAction(handled=True, prompt=prompt)


def _is_repl_exit_input(inp: str) -> bool:
    """判断用户输入是否是退出 REPL 的命令。"""
    return inp.strip() in ("exit", "quit", "/bye")


def _process_repl_input(
    inp: str,
    *,
    workdir: str,
    loaded_skills: list[str],
    working: list,
    ui: TerminalUI,
    max_turns: int,
    enable_mcp: bool,
    session: Session | None = None,
    contract_controller: ContractController | None = None,
) -> tuple[str, bool, Session | None]:
    """处理一条 REPL 输入；返回新的 workdir、退出状态和当前 session。"""
    inp = inp.strip()
    if not inp:
        return workdir, False, session

    action = handle_repl_command(
        inp,
        workdir=workdir,
        loaded_skills=loaded_skills,
        working=working,
        ui=ui,
        session=session,
        contract_controller=contract_controller,
    )
    if action.exit_requested:
        return workdir, True, action.session or session
    if action.workdir is not None:
        return action.workdir, False, action.session or session
    if action.handled and action.prompt is None:
        return workdir, False, action.session or session

    prompt = action.prompt if action.prompt is not None else inp
    try:
        ocr_prompt = expand_image_markers_to_ocr_prompt(prompt)
    except OcrError as exc:
        ui.show_error(str(exc))
        log_event(LOGGER, logging.WARNING, "repl_image_marker_ocr_failed", error=str(exc))
        return workdir, False, action.session or session
    if ocr_prompt is not None:
        prompt = ocr_prompt
        log_event(LOGGER, logging.INFO, "repl_image_marker_ocr_completed")
    if action.prompt is None:
        log_event(LOGGER, logging.INFO, "repl_prompt_received", prompt_chars=len(inp))

    if session is not None:
        session.record_prompt(prompt)
    _append_context_message(
        working,
        {"role": "user", "content": prompt},
        session=session,
    )
    ui.show_user_message(prompt)
    base_sys = build_agent_prompt(loaded_skills, workdir)
    run_kwargs = {
        "max_turns": max_turns,
        "ui": ui,
        "enable_mcp": enable_mcp,
    }
    if session is not None:
        run_kwargs["session"] = session
    if contract_controller is not None:
        run_kwargs["contract_controller"] = contract_controller
    run_loop(base_sys, working, workdir, **run_kwargs)
    compaction = compact_context(
        working,
        workdir=workdir,
        model=get_model_name(),
        instructions=base_sys.instructions,
        fixed_messages=base_sys.context_messages,
        reason="repl_post_turn",
    )
    _apply_compaction_result(working, compaction, session=session)
    _show_context_usage(ui, compaction)
    return workdir, False, session


def _run_repl_sync(
    *,
    workdir: str,
    loaded_skills: list[str],
    working: list,
    ui: TerminalUI,
    max_turns: int,
    enable_mcp: bool,
    session: Session | None = None,
    contract_controller: ContractController | None = None,
) -> None:
    """非交互输入流使用的同步 REPL，保持管道和测试行为稳定。"""
    current_session = session
    while True:
        try:
            inp = ui.read_prompt(
                repl_completions(workdir, loaded_skills),
                workdir=workdir,
            )
        except (EOFError, KeyboardInterrupt):
            _show_resume_command(ui, workdir, current_session)
            ui.blank_line()
            break

        workdir, exit_requested, current_session = _process_repl_input(
            inp,
            workdir=workdir,
            loaded_skills=loaded_skills,
            working=working,
            ui=ui,
            max_turns=max_turns,
            enable_mcp=enable_mcp,
            session=current_session,
            contract_controller=contract_controller,
        )
        if exit_requested:
            break


def _discard_pending_inputs(input_queue: Queue[str | None]) -> None:
    """丢弃尚未开始执行的排队输入，用于用户退出或 Ctrl-C。"""
    while True:
        try:
            input_queue.get_nowait()
        except Empty:
            return
        input_queue.task_done()


def _run_repl_with_input_queue(
    *,
    workdir: str,
    loaded_skills: list[str],
    working: list,
    ui: TerminalUI,
    max_turns: int,
    enable_mcp: bool,
    session: Session | None = None,
    contract_controller: ContractController | None = None,
) -> None:
    """交互式 REPL：主线程继续读输入，后台 worker 顺序执行 AI 工作。"""
    input_queue: Queue[str | None] = Queue()
    stop_requested = Event()
    active_task = Event()
    worker_busy = Event()
    worker_errors: list[BaseException] = []
    current_workdir = workdir
    current_session = session

    def worker() -> None:
        nonlocal current_workdir, current_session
        while True:
            item = input_queue.get()
            try:
                if item is None:
                    return
                worker_busy.set()
                current_workdir, exit_requested, current_session = _process_repl_input(
                    item,
                    workdir=current_workdir,
                    loaded_skills=loaded_skills,
                    working=working,
                    ui=ui,
                    max_turns=max_turns,
                    enable_mcp=enable_mcp,
                    session=current_session,
                    contract_controller=contract_controller,
                )
                if exit_requested:
                    stop_requested.set()
                    return
            except BaseException as exc:  # pragma: no cover - 主线程会重新抛出
                worker_errors.append(exc)
                stop_requested.set()
                return
            finally:
                worker_busy.clear()
                if input_queue.empty():
                    active_task.clear()
                input_queue.task_done()

    worker_thread = Thread(target=worker, name="dong-repl-worker", daemon=False)
    worker_thread.start()
    ui.set_background_input_mode(True)
    try:
        while not stop_requested.is_set():
            if worker_errors:
                raise worker_errors[0]

            try:
                is_busy = (
                    active_task.is_set()
                    or worker_busy.is_set()
                    or input_queue.qsize() > 0
                )
                inp = ui.read_prompt(
                    repl_completions(current_workdir, loaded_skills),
                    prompt_text="\ndong next " if is_busy else "\ndong ",
                    bottom_toolbar="AI 正在工作；当前输入会排队到下一轮"
                    if is_busy
                    else None,
                    workdir=current_workdir,
                )
            except (EOFError, KeyboardInterrupt):
                _show_resume_command(ui, current_workdir, current_session)
                ui.blank_line()
                stop_requested.set()
                _discard_pending_inputs(input_queue)
                input_queue.put(None)
                break

            inp = inp.strip()
            if not inp:
                continue

            if _is_repl_exit_input(inp):
                stop_requested.set()
                _discard_pending_inputs(input_queue)
                input_queue.put(inp)
                break

            should_show_queued = worker_busy.is_set() or input_queue.qsize() > 0
            active_task.set()
            input_queue.put(inp)
            if should_show_queued:
                ui.show_input_queued(pending=input_queue.qsize())
    finally:
        ui.set_background_input_mode(False)
        if not stop_requested.is_set():
            stop_requested.set()
            _discard_pending_inputs(input_queue)
            input_queue.put(None)
        worker_thread.join()
        if worker_errors:
            raise worker_errors[0]


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
    parser.add_argument("-d", "--dir", default=None, help="Working directory")
    parser.add_argument("--file", help="Log file path under working directory")
    parser.add_argument(
        "--limit", type=_positive_int, default=200, help="Max matching lines"
    )
    parser.add_argument("--level", help="Filter by level, e.g. INFO or WARNING")
    parser.add_argument("--event", help="Filter by exact event name")
    parser.add_argument("--logger", help="Filter by exact logger name, e.g. dong.tools")
    parser.add_argument("--contains", help="Filter by raw line substring")
    parser.add_argument("--json", action="store_true", help="Emit JSON lines")
    parser.add_argument(
        "--follow", action="store_true", help="Follow appended log lines"
    )
    parser.add_argument(
        "--interval", type=_positive_float, default=1.0, help="Follow interval seconds"
    )
    args = parser.parse_args(argv)

    workdir = _resolve_workdir(args.dir or ".", explicit=args.dir is not None)
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

    parser = argparse.ArgumentParser(
        description="dong mcp — inspect project MCP servers"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser(
        "list", help="List configured MCP servers and tools"
    )
    list_parser.add_argument("-d", "--dir", default=None, help="Working directory")
    list_parser.add_argument(
        "--tools",
        action="store_true",
        help="Connect to enabled servers and list discovered tools",
    )
    args = parser.parse_args(argv)

    if args.command == "list":
        workdir = _resolve_workdir(args.dir or ".", explicit=args.dir is not None)
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


def _open_session(
    *,
    workdir: str,
    model: str,
    resume: str | None,
    ui: TerminalUI,
) -> Session:
    """创建或恢复当前工作区 session；CLI 入口统一在这里处理错误展示。"""
    store = SessionStore(workdir)
    try:
        if resume is None:
            return store.create(model=model)
        return store.load(resume)
    except SessionError as exc:
        ui.show_error(str(exc))
        raise SystemExit(2) from exc


def main():
    """CLI 主入口：解析参数，并根据是否传入 prompt 选择单次或 REPL 模式。"""
    import argparse

    if len(sys.argv) > 1 and sys.argv[1] == "logs":
        run_logs_cli(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "mcp":
        run_mcp_cli(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "help":
        # 把 "help" 子命令重定向到 argparse 的 --help
        sys.argv = [sys.argv[0], "--help"]

    # 解析 CLI 参数：模型、工作目录、最大轮数、OCR 图片、预加载 skill、一次性 prompt。
    parser = argparse.ArgumentParser(description="dong — a minimal CLI coding agent")
    parser.add_argument("-m", "--model", default=get_model_name(), help="LLM model")
    parser.add_argument("-d", "--dir", default=None, help="Working directory")
    parser.add_argument(
        "-t",
        "--max-turns",
        type=int,
        default=200,
        help="Max agent turns before giving up (default: 200)",
    )
    parser.add_argument(
        "--skill", action="append", default=[], help="Load a skill by name"
    )
    parser.add_argument(
        "--mcp", action="store_true", help="Enable configured stdio MCP servers"
    )
    parser.add_argument(
        "--image",
        metavar="PATH",
        help="Run local OCR on an image, then send recognized text with the prompt",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        default=None,
        metavar="SESSION",
        help="Resume a session in the current workdir (default: latest)",
    )
    parser.add_argument("input", nargs="*", help="Single prompt. Omit for REPL.")
    args = parser.parse_args()
    ui = TerminalUI()

    # 把模型放到环境变量里，后续 LLM 调用可以统一读取 DONG_MODEL。
    os.environ["DONG_MODEL"] = args.model

    # 统一转成绝对路径，避免后续文件工具在相对路径上产生歧义。
    workdir = _resolve_workdir(args.dir or ".", explicit=args.dir is not None)
    log_path = configure_logging(workdir)
    log_event(
        LOGGER,
        logging.INFO,
        "cli_started",
        model=args.model,
        workdir=workdir,
        mode="single" if args.input or args.image else "repl",
        log_path=str(log_path) if log_path else None,
        resume=args.resume,
    )

    session = _open_session(
        workdir=workdir,
        model=args.model,
        resume=args.resume,
        ui=ui,
    )

    # 加载项目规则；DONG.md 会进入系统消息，约束 agent 的行为。
    project_rules = load_dong_md(workdir)
    interactive_tui = not args.input and not args.image and ui._interactive()
    tool_names = [
        *(tool["function"]["name"] for tool in TOOL_DEFS),
        *(["mcp"] if args.mcp else []),
    ]
    if not interactive_tui:
        ui.show_startup(
            model=args.model,
            workdir=workdir,
            agents_loaded=project_rules is not None,
            tools=tool_names,
        )

    # 保存当前已启用的 skill 名称；build_messages 会根据它们拼装系统提示词。
    loaded_skills = []
    startup_loaded_skills: list[SkillInfo] = []

    # 处理命令行里重复传入的 --skill，例如：--skill python --skill git。
    for name in args.skill:
        try:
            info, _ = load_skill(workdir, name)
            if info.name not in loaded_skills:
                loaded_skills.append(info.name)
            if interactive_tui:
                startup_loaded_skills.append(info)
            else:
                ui.show_loaded_skill(info.name, info.selected_source, indent="   ")
        except FileNotFoundError as e:
            ui.show_skill_error(e)
            raise SystemExit(2) from e

    if args.input or args.image:
        # 单次执行模式：命令行里带了 prompt，跑完一轮 run_loop 后直接退出。
        working = session.messages
        user_prompt = " ".join(args.input)
        if args.image:
            try:
                ocr_result = extract_text_from_image(args.image)
                user_prompt = build_ocr_prompt(ocr_result, user_prompt)
                log_event(
                    LOGGER,
                    logging.INFO,
                    "single_ocr_completed",
                    image_path=ocr_result.image_path,
                    text_chars=len(ocr_result.text),
                    lines=len(ocr_result.lines),
                )
            except OcrError as exc:
                ui.show_error(str(exc))
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "single_ocr_failed",
                    image_path=args.image,
                    error=str(exc),
                )
                raise SystemExit(2) from exc
        else:
            try:
                ocr_prompt = expand_image_markers_to_ocr_prompt(user_prompt)
            except OcrError as exc:
                ui.show_error(str(exc))
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "single_image_marker_ocr_failed",
                    error=str(exc),
                )
                raise SystemExit(2) from exc
            if ocr_prompt is not None:
                user_prompt = ocr_prompt
        log_event(
            LOGGER,
            logging.INFO,
            "single_prompt_received",
            prompt_chars=len(user_prompt),
        )
        session.record_prompt(user_prompt)
        _append_context_message(
            working,
            {"role": "user", "content": user_prompt},
            session=session,
        )
        ui.show_user_message(user_prompt)
        base_sys = build_agent_prompt(loaded_skills, workdir)
        run_loop(
            base_sys,
            working,
            workdir,
            max_turns=args.max_turns,
            ui=ui,
            enable_mcp=args.mcp,
            session=session,
        )
    else:
        # REPL 模式：没有一次性 prompt，就进入交互循环，持续保留 working 上下文。
        avail = describe_skills(workdir)
        log_event(LOGGER, logging.INFO, "repl_started", skill_count=len(avail))
        working = session.messages
        contract_controller = ContractController(workdir=workdir)
        if interactive_tui:
            from dong.tui import TuiApp

            def process_tui_input(inp: str, tui_ui) -> bool:  # type: ignore[no-untyped-def]
                nonlocal workdir, session
                workdir, exit_requested, session = _process_repl_input(
                    inp,
                    workdir=workdir,
                    loaded_skills=loaded_skills,
                    working=working,
                    ui=tui_ui,
                    max_turns=args.max_turns,
                    enable_mcp=args.mcp,
                    session=session,
                    contract_controller=contract_controller,
                )
                return exit_requested

            tui_app = TuiApp(
                process_input=process_tui_input,
                completion_provider=lambda: repl_completions(workdir, loaded_skills),
                resume_command_provider=lambda: _session_resume_command(
                    workdir, session.session_id
                ),
                workdir=workdir,
            )
            tui_app.ui.show_startup(
                model=args.model,
                workdir=workdir,
                agents_loaded=project_rules is not None,
                tools=tool_names,
            )
            for info in startup_loaded_skills:
                tui_app.ui.show_loaded_skill(
                    info.name, info.selected_source, indent="   "
                )
            tui_app.ui.show_repl_help(skill_count=len(avail))
            tui_app.ui.show_session_resume_command(
                _session_resume_command(workdir, session.session_id)
            )
            previous_sigint = signal.getsignal(signal.SIGINT)

            def handle_tui_sigint(_signum, _frame) -> None:  # type: ignore[no-untyped-def]
                raise SystemExit(0)

            signal.signal(signal.SIGINT, handle_tui_sigint)
            try:
                tui_app.run()
            finally:
                signal.signal(signal.SIGINT, previous_sigint)
        else:
            ui.show_repl_help(skill_count=len(avail))
            _run_repl_sync(
                workdir=workdir,
                loaded_skills=loaded_skills,
                working=working,
                ui=ui,
                max_turns=args.max_turns,
                enable_mcp=args.mcp,
                session=session,
                contract_controller=contract_controller,
            )


if __name__ == "__main__":
    main()
