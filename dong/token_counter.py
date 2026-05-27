"""Token 计数 Module：按模型选择官方或 tiktoken 计数路径。"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from dong.logging_config import get_logger, log_event

LOGGER = get_logger(__name__)
DEFAULT_TIKTOKEN_ENCODING = "o200k_base"
DEEPSEEK_TOKENIZER_DIRS: dict[str, str] = {
    "deepseek-v4-pro": "deepseek_v3",
    "deepseek-v4-flash": "deepseek_v3",
}


def _canonical_model(model: str | None) -> str:
    """规整模型名，去掉 provider 前缀并统一大小写。"""
    selected = (model or os.getenv("DONG_MODEL") or "deepseek-v4-pro").strip().lower()
    return selected.rsplit("/", 1)[-1]


def _is_deepseek_model(model: str) -> bool:
    """判断模型是否应优先使用 DeepSeek 官方 tokenizer。"""
    return model.startswith("deepseek-")


def _is_claude_model(model: str) -> bool:
    """判断模型是否支持 Anthropic count_tokens 官方计数接口。"""
    return model.startswith("claude-")


def _request_payload(
    messages: list,
    *,
    instructions: str = "",
    tools: list | None = None,
) -> dict[str, Any]:
    """构造稳定的请求计数载荷，供 tiktoken 近似路径使用。"""
    return {
        "instructions": instructions,
        "messages": messages,
        "tools": tools or [],
    }


def _json_dumps(value: Any) -> str:
    """把非字符串对象转成稳定 JSON 文本，避免退回旧的字节预算。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


@lru_cache(maxsize=64)
def _tiktoken_encoding(model: str):
    """读取 tiktoken encoding，并标记是否退到了默认 encoding。"""
    try:
        import tiktoken
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "tiktoken is required for non-official token counting paths. "
            "Reinstall dong so pyproject dependencies are available.",
        ) from exc
    try:
        return tiktoken.encoding_for_model(model), False
    except KeyError:
        return tiktoken.get_encoding(DEFAULT_TIKTOKEN_ENCODING), True


def _count_tiktoken_text(
    text: str,
    *,
    model: str,
    reason: str,
    approximate: bool,
) -> int:
    """用 tiktoken 计算文本 token，并在近似路径写日志。"""
    encoding, encoding_fallback = _tiktoken_encoding(model)
    if approximate or encoding_fallback:
        log_event(
            LOGGER,
            logging.INFO,
            "token_counter_approximate",
            model=model,
            encoding=getattr(encoding, "name", DEFAULT_TIKTOKEN_ENCODING),
            reason=reason if approximate else "unknown_tiktoken_model",
        )
    return len(encoding.encode(text))


def _count_tiktoken_request(
    messages: list,
    *,
    instructions: str,
    tools: list | None,
    model: str,
    reason: str,
    approximate: bool,
) -> int:
    """用 tiktoken 计算完整请求载荷 token。"""
    return _count_tiktoken_text(
        _json_dumps(_request_payload(messages, instructions=instructions, tools=tools)),
        model=model,
        reason=reason,
        approximate=approximate,
    )


def _deepseek_tokenizer_dir(model: str) -> Path:
    """定位 DeepSeek 官方 tokenizer 目录；环境变量可覆盖内置资产路径。"""
    override = os.getenv("DONG_DEEPSEEK_TOKENIZER_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    tokenizer_name = DEEPSEEK_TOKENIZER_DIRS.get(model, "deepseek_v3")
    return Path(__file__).resolve().parent / "tokenizers" / tokenizer_name


@lru_cache(maxsize=8)
def _load_deepseek_tokenizer(model: str):
    """懒加载 DeepSeek tokenizer，避免启动 CLI 时加载 transformers。"""
    tokenizer_dir = _deepseek_tokenizer_dir(model)
    if not tokenizer_dir.exists():
        raise FileNotFoundError(f"DeepSeek tokenizer dir not found: {tokenizer_dir}")
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(
        str(tokenizer_dir),
        trust_remote_code=True,
        local_files_only=True,
    )


def _message_for_chat_template(message: Any) -> dict[str, Any]:
    """把 SDK/dict 消息规整成 chat template 可读取的基础形状。"""
    if isinstance(message, dict):
        role = message.get("role", "user")
        content = message.get("content", "")
        converted = {"role": role, "content": content}
        if message.get("tool_calls"):
            converted["tool_calls"] = message["tool_calls"]
        if message.get("tool_call_id"):
            converted["tool_call_id"] = message["tool_call_id"]
        return converted
    return {
        "role": getattr(message, "role", "user"),
        "content": getattr(message, "content", ""),
    }


def _chat_template_messages(messages: list, instructions: str) -> list[dict[str, Any]]:
    """把 instructions 合并成 system message 后交给模型 chat template。"""
    converted = []
    if instructions:
        converted.append({"role": "system", "content": instructions})
    converted.extend(_message_for_chat_template(message) for message in messages)
    return converted


def _count_deepseek_request(
    messages: list,
    *,
    instructions: str,
    tools: list | None,
    model: str,
) -> int:
    """优先用 DeepSeek 官方 tokenizer 计算 chat 请求 token。"""
    try:
        tokenizer = _load_deepseek_tokenizer(model)
        token_ids = tokenizer.apply_chat_template(
            _chat_template_messages(messages, instructions),
            tokenize=True,
            add_generation_prompt=True,
        )
        total = len(token_ids)
        if tools:
            total += len(tokenizer.encode(_json_dumps(tools)))
        return total
    except Exception as exc:
        return _count_tiktoken_request(
            messages,
            instructions=instructions,
            tools=tools,
            model=model,
            reason=f"deepseek_tokenizer_unavailable:{type(exc).__name__}",
            approximate=True,
        )


def _count_claude_request(
    messages: list,
    *,
    instructions: str,
    tools: list | None,
    model: str,
) -> int:
    """优先调用 Anthropic count_tokens；失败时退到 tiktoken 近似。"""
    if not (os.getenv("DONG_ANTHROPIC_API_KEY") or os.getenv("DONG_API_KEY")):
        return _count_tiktoken_request(
            messages,
            instructions=instructions,
            tools=tools,
            model=model,
            reason="missing_anthropic_api_key",
            approximate=True,
        )
    try:
        from dong.llm import LlmRequest, _anthropic_request_kwargs, _get_anthropic_client

        kwargs = _anthropic_request_kwargs(
            LlmRequest(
                model=model,
                instructions=instructions,
                input=messages,
                tools=tools or [],
            ),
        )
        kwargs.pop("max_tokens", None)
        kwargs.pop("temperature", None)
        result = _get_anthropic_client().messages.count_tokens(**kwargs)
        return int(getattr(result, "input_tokens"))
    except Exception as exc:
        return _count_tiktoken_request(
            messages,
            instructions=instructions,
            tools=tools,
            model=model,
            reason=f"anthropic_count_tokens_failed:{type(exc).__name__}",
            approximate=True,
        )


def count_text_tokens(text: str, *, model: str | None = None) -> int:
    """计算纯文本 token；非官方路径统一使用 tiktoken。"""
    selected_model = _canonical_model(model)
    if _is_deepseek_model(selected_model):
        try:
            return len(_load_deepseek_tokenizer(selected_model).encode(text))
        except Exception as exc:
            return _count_tiktoken_text(
                text,
                model=selected_model,
                reason=f"deepseek_tokenizer_unavailable:{type(exc).__name__}",
                approximate=True,
            )
    approximate = not selected_model.startswith(("gpt-", "o1", "o3", "o4"))
    return _count_tiktoken_text(
        text,
        model=selected_model,
        reason="generic_tiktoken_fallback",
        approximate=approximate,
    )


def count_request_tokens(
    messages: list,
    *,
    instructions: str = "",
    tools: list | None = None,
    model: str | None = None,
) -> int:
    """计算一次 LLM 请求的输入 token，供上下文压缩 preflight 使用。"""
    selected_model = _canonical_model(model)
    if _is_deepseek_model(selected_model):
        return _count_deepseek_request(
            messages,
            instructions=instructions,
            tools=tools,
            model=selected_model,
        )
    if _is_claude_model(selected_model):
        return _count_claude_request(
            messages,
            instructions=instructions,
            tools=tools,
            model=selected_model,
        )
    approximate = not selected_model.startswith(("gpt-", "o1", "o3", "o4"))
    return _count_tiktoken_request(
        messages,
        instructions=instructions,
        tools=tools,
        model=selected_model,
        reason="generic_tiktoken_fallback",
        approximate=approximate,
    )
