"""上下文压缩 Module：集中处理 dong 对话历史预算、摘要和保留策略。"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from dong.logging_config import get_logger, log_event
from dong.token_counter import count_request_tokens
from dong.tools import _validate_path

LOGGER = get_logger(__name__)

CONTEXT_SUMMARY_RELPATH = ".dong/context"
CONTEXT_SUMMARY_PREFIX = "--- Compacted conversation context ---"
DEFAULT_CONTEXT_MAX_MESSAGES = 20
DEFAULT_AUTO_COMPACT_PERCENT = 80
DEFAULT_MANUAL_COMPACT_TOKENS = 10_000
DEFAULT_MAX_OUTPUT_TOKENS = 4096
MIN_TAIL_MESSAGES = 4


@dataclass(frozen=True)
class ModelContextLimit:
    """模型上下文窗口定义；用于把压缩触发从固定字符数升级为模型感知预算。"""

    context_window_tokens: int
    max_output_tokens: int


@dataclass(frozen=True)
class ContextBudget:
    """一次压缩判断使用的 token 预算。"""

    limit: int
    model: str
    context_window_tokens: int | None = None
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS


@dataclass(frozen=True)
class ContextCompactionResult:
    """上下文压缩结果；调用方只需要替换 messages 并读取统计信息。"""

    messages: list
    compacted: bool
    removed_messages: int
    preserved_messages: int
    summary_ref: str | None
    estimated_tokens_before: int
    estimated_tokens_after: int
    budget: ContextBudget


MODEL_CONTEXT_LIMITS: dict[str, ModelContextLimit] = {
    "deepseek-v4-pro": ModelContextLimit(1_000_000, 8192),
    "deepseek-v4-flash": ModelContextLimit(1_000_000, 8192),
    "claude-sonnet-4-20250514": ModelContextLimit(200_000, 64_000),
    "claude-sonnet-4-6": ModelContextLimit(200_000, 64_000),
    "claude-opus-4-6": ModelContextLimit(200_000, 32_000),
    "gpt-4.1": ModelContextLimit(1_047_576, 32_768),
    "gpt-4.1-mini": ModelContextLimit(1_047_576, 32_768),
    "gpt-4.1-nano": ModelContextLimit(1_047_576, 32_768),
    "gpt-5.4": ModelContextLimit(1_000_000, 128_000),
    "gpt-5.4-mini": ModelContextLimit(400_000, 128_000),
    "gpt-5.4-nano": ModelContextLimit(400_000, 128_000),
}


def _canonical_model(model: str | None) -> str:
    """规整模型名；兼容 openai/deepseek-v4-pro 这类 provider 前缀。"""
    selected = (model or os.getenv("DONG_MODEL") or "deepseek-v4-pro").strip().lower()
    return selected.rsplit("/", 1)[-1]


def model_context_limit(model: str | None) -> ModelContextLimit | None:
    """读取内置模型上下文窗口；未知模型使用保守 token 预算。"""
    return MODEL_CONTEXT_LIMITS.get(_canonical_model(model))


def _env_int(name: str, *, default: int, minimum: int) -> int:
    """读取整数环境变量；非法或过小值回退默认值。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def _auto_compact_percent() -> int:
    """读取模型窗口自动压缩百分比，限制在 10%-95% 避免过激或无效配置。"""
    value = _env_int(
        "DONG_CONTEXT_AUTO_COMPACT_PERCENT",
        default=DEFAULT_AUTO_COMPACT_PERCENT,
        minimum=10,
    )
    return min(value, 95)


def _max_output_tokens(limit: ModelContextLimit | None) -> int:
    """读取最大输出 token 预算；默认跟随模型表，环境变量可降低或提高。"""
    default = limit.max_output_tokens if limit else DEFAULT_MAX_OUTPUT_TOKENS
    return _env_int("DONG_MAX_TOKENS", default=default, minimum=1)


def build_context_budget(model: str | None = None) -> ContextBudget:
    """构建 token 压缩预算；已知模型使用上下文窗口，未知模型使用保守阈值。"""
    selected_model = _canonical_model(model)
    limit = model_context_limit(selected_model)
    explicit_token_limit = os.getenv("DONG_CONTEXT_MAX_TOKENS", "").strip()
    if explicit_token_limit:
        return ContextBudget(
            limit=_env_int(
                "DONG_CONTEXT_MAX_TOKENS",
                default=DEFAULT_MANUAL_COMPACT_TOKENS,
                minimum=1_000,
            ),
            model=selected_model,
            context_window_tokens=limit.context_window_tokens if limit else None,
            max_output_tokens=_max_output_tokens(limit),
        )

    if limit is None:
        return ContextBudget(
            limit=DEFAULT_MANUAL_COMPACT_TOKENS,
            model=selected_model,
            max_output_tokens=_max_output_tokens(limit),
        )

    return ContextBudget(
        limit=(limit.context_window_tokens * _auto_compact_percent()) // 100,
        model=selected_model,
        context_window_tokens=limit.context_window_tokens,
        max_output_tokens=_max_output_tokens(limit),
    )


def _role(message: Any) -> str:
    """兼容 dict 和 SDK 消息对象读取 role。"""
    if isinstance(message, dict):
        return message.get("role", "")
    return getattr(message, "role", "")


def _tool_calls(message: Any) -> list:
    """兼容 dict 和 SDK 消息对象读取 assistant tool_calls。"""
    if isinstance(message, dict):
        return message.get("tool_calls") or []
    return getattr(message, "tool_calls", None) or []


def _message_content(message: Any) -> str:
    """读取消息正文；结构化内容转成 JSON，方便统一估算和摘要。"""
    if isinstance(message, dict):
        content = message.get("content", "")
    else:
        content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _reasoning_content(message: Any) -> str:
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


def message_reasoning_content(message: Any) -> str:
    """公开读取 reasoning_content，供 CLI 日志和 UI 展示复用同一规则。"""
    return _reasoning_content(message)


def _message_tool_call_text(message: Any) -> str:
    """把工具调用名称和参数纳入预算，避免隐藏大参数撑爆上下文。"""
    parts = []
    for tool_call in _tool_calls(message):
        if isinstance(tool_call, dict):
            function = tool_call.get("function") or {}
            parts.append(str(function.get("name", "")))
            parts.append(str(function.get("arguments", "")))
        else:
            function = tool_call.function
            parts.append(str(function.name))
            parts.append(str(function.arguments))
    return "\n".join(parts)


def estimate_request_tokens(
    messages: list,
    *,
    instructions: str = "",
    tools: list | None = None,
    model: str | None = None,
) -> int:
    """估算一次 LLM 请求的输入 token，覆盖 instructions、messages 和 tools。"""
    return count_request_tokens(
        messages,
        instructions=instructions,
        tools=tools,
        model=model,
    )


def _tool_call_id(tool_call: Any) -> str:
    """兼容 dict 和 SDK 对象读取 tool_call id。"""
    return tool_call.get("id", "") if isinstance(tool_call, dict) else tool_call.id


def _tool_result_id(message: Any) -> str | None:
    """读取 tool result 对应的 tool_call_id。"""
    if isinstance(message, dict):
        return message.get("tool_call_id")
    return getattr(message, "tool_call_id", None)


def _safe_tail_start(messages: list, keep_from: int) -> int:
    """把保留边界回退到安全点，避免拆散 assistant tool_call 与 tool result。"""
    k = max(0, min(keep_from, len(messages)))
    while 0 < k < len(messages) and _role(messages[k]) == "tool":
        k -= 1
        if _role(messages[k]) == "assistant":
            break
    return k


def _advance_tail_start(
    messages: list,
    keep_from: int,
    *,
    min_tail_messages: int = MIN_TAIL_MESSAGES,
) -> int | None:
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
            for tool_call in _tool_calls(message):
                tool_call_id = _tool_call_id(tool_call)
                if tool_call_id:
                    valid_ids.add(tool_call_id)
            result.append(message)
        elif role == "tool":
            if _tool_result_id(message) in valid_ids:
                result.append(message)
        else:
            result.append(message)
    return result


def _summarize_message(message: Any) -> str:
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


def _extract_summary_user_requests(content: str) -> list[str]:
    """从旧压缩摘要继承用户目标，避免二次压缩把任务意图丢掉。"""
    requests = []
    in_recent_user_section = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped in ("- 最近用户请求:", "- 最近用户请求："):
            in_recent_user_section = True
            continue
        if not in_recent_user_section:
            continue
        if line.startswith("  - user:") or stripped.startswith("- user:"):
            requests.append(stripped)
            continue
        if stripped.startswith("- "):
            break
    return requests


def _recent_user_requests(messages: list) -> list[str]:
    """汇总直接用户消息和旧摘要中的用户目标，保留最近 3 条去重结果。"""
    requests = []
    for message in messages:
        role = _role(message)
        if role == "user":
            requests.append(_summarize_message(message))
        elif role == "system":
            requests.extend(_extract_summary_user_requests(_message_content(message)))

    deduped = []
    for request in requests:
        if request not in deduped:
            deduped.append(request)
    return deduped[-3:]


def _build_context_summary(removed: list) -> str:
    """生成确定性上下文摘要；不用额外 LLM 调用，确保离线可测试。"""
    counts: dict[str, int] = {}
    tools: set[str] = set()
    for message in removed:
        role = _role(message) or "unknown"
        counts[role] = counts.get(role, 0) + 1
        for tool_call in _tool_calls(message):
            if isinstance(tool_call, dict):
                name = (tool_call.get("function") or {}).get("name")
            else:
                name = tool_call.function.name
            if name:
                tools.add(str(name))

    recent_user = _recent_user_requests(removed)
    timeline = [_summarize_message(message) for message in removed[-8:]]

    lines = [
        CONTEXT_SUMMARY_PREFIX,
        "说明：以下内容由 dong 在本地根据旧消息自动压缩生成，不依赖数据库或额外 LLM 调用。",
        f"- 压缩消息数：{len(removed)}",
        "- 角色分布："
        + ", ".join(f"{role}={count}" for role, count in sorted(counts.items())),
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
    with open(path, "w", encoding="utf-8") as file:
        file.write(summary)
        file.write("\n")
    return relpath


def _estimated_tokens(
    fixed_messages: list,
    working_messages: list,
    *,
    instructions: str,
    tools: list | None,
    budget: ContextBudget,
) -> int:
    """估算完整请求 token，并把输出预算纳入 token 型窗口判断。"""
    input_tokens = estimate_request_tokens(
        [*fixed_messages, *working_messages],
        instructions=instructions,
        tools=tools,
        model=budget.model,
    )
    return input_tokens + budget.max_output_tokens


def _over_budget(
    fixed_messages: list,
    working_messages: list,
    *,
    instructions: str,
    tools: list | None,
    budget: ContextBudget,
) -> bool:
    """判断当前上下文是否超过 token 预算。"""
    return (
        _estimated_tokens(
            fixed_messages,
            working_messages,
            instructions=instructions,
            tools=tools,
            budget=budget,
        )
        >= budget.limit
    )


def compact_context(
    messages: list,
    *,
    max_len: int = DEFAULT_CONTEXT_MAX_MESSAGES,
    workdir: str | None = None,
    model: str | None = None,
    instructions: str = "",
    tools: list | None = None,
    fixed_messages: list | None = None,
    force: bool = False,
    preserve_recent_messages: int | None = None,
    reason: str = "budget",
) -> ContextCompactionResult:
    """按消息数和模型预算压缩上下文，同时保留 tool_call/tool result 配对。"""
    budget = build_context_budget(model)
    fixed = fixed_messages or []
    estimated_tokens_before = _estimated_tokens(
        fixed,
        messages,
        instructions=instructions,
        tools=tools,
        budget=budget,
    )
    too_many_messages = len(messages) > max_len
    over_budget_now = _over_budget(
        fixed,
        messages,
        instructions=instructions,
        tools=tools,
        budget=budget,
    )
    should_compact = force or too_many_messages or over_budget_now
    if not should_compact:
        return ContextCompactionResult(
            messages=messages,
            compacted=False,
            removed_messages=0,
            preserved_messages=len(messages),
            summary_ref=None,
            estimated_tokens_before=estimated_tokens_before,
            estimated_tokens_after=estimated_tokens_before,
            budget=budget,
        )

    if preserve_recent_messages is not None:
        target_tail = preserve_recent_messages
    elif too_many_messages:
        target_tail = max_len
    elif over_budget_now:
        target_tail = MIN_TAIL_MESSAGES
    else:
        target_tail = max_len
    keep_from = _safe_tail_start(messages, len(messages) - max(0, target_tail))
    tail = messages[keep_from:]

    while len(tail) > MIN_TAIL_MESSAGES and _over_budget(
        fixed,
        tail,
        instructions=instructions,
        tools=tools,
        budget=budget,
    ):
        next_keep_from = _advance_tail_start(messages, keep_from)
        if next_keep_from is None:
            break
        keep_from = next_keep_from
        tail = messages[keep_from:]

    removed = messages[:keep_from]
    if not removed:
        compacted_messages = _drop_orphan_tool_results(tail)
        estimated_tokens_after = _estimated_tokens(
            fixed,
            compacted_messages,
            instructions=instructions,
            tools=tools,
            budget=budget,
        )
        return ContextCompactionResult(
            messages=compacted_messages,
            compacted=False,
            removed_messages=0,
            preserved_messages=len(compacted_messages),
            summary_ref=None,
            estimated_tokens_before=estimated_tokens_before,
            estimated_tokens_after=estimated_tokens_after,
            budget=budget,
        )

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
    compacted_messages = _drop_orphan_tool_results([summary_message, *tail])
    estimated_tokens_after = _estimated_tokens(
        fixed,
        compacted_messages,
        instructions=instructions,
        tools=tools,
        budget=budget,
    )
    log_event(
        LOGGER,
        logging.INFO,
        "context_compacted",
        reason=reason,
        model=budget.model,
        budget_limit=budget.limit,
        removed_messages=len(removed),
        preserved_messages=len(tail),
        estimated_tokens_before=estimated_tokens_before,
        estimated_tokens_after=estimated_tokens_after,
        summary_ref=summary_ref,
    )
    return ContextCompactionResult(
        messages=compacted_messages,
        compacted=True,
        removed_messages=len(removed),
        preserved_messages=len(tail),
        summary_ref=summary_ref,
        estimated_tokens_before=estimated_tokens_before,
        estimated_tokens_after=estimated_tokens_after,
        budget=budget,
    )


def trim_context(
    messages: list,
    max_len: int = DEFAULT_CONTEXT_MAX_MESSAGES,
    workdir: str | None = None,
    *,
    model: str | None = None,
    instructions: str = "",
    tools: list | None = None,
    fixed_messages: list | None = None,
    force: bool = False,
    preserve_recent_messages: int | None = None,
    reason: str = "budget",
) -> list:
    """兼容旧调用的浅接口；CLI 内部统计路径应使用 compact_context。

    旧调用方是 `dong.cli.trim_context` 导入和现有 CLI e2e 测试；等外部/测试不再
    依赖浅返回值时，可删除该 wrapper，只保留 `compact_context()`。
    """
    return compact_context(
        messages,
        max_len=max_len,
        workdir=workdir,
        model=model,
        instructions=instructions,
        tools=tools,
        fixed_messages=fixed_messages,
        force=force,
        preserve_recent_messages=preserve_recent_messages,
        reason=reason,
    ).messages
