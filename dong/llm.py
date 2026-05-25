"""LLM API 封装：兼容 OpenAI 接口，并自动读取项目根目录的 .env。"""
import logging
import os
import pathlib
import time

from openai import OpenAI

from dong.logging_config import get_logger, log_event

LOGGER = get_logger(__name__)

# 自动加载项目根目录的 .env，避免为了一个简单场景额外引入依赖。
_env_path = pathlib.Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

_client = None


def _deepseek_request_options() -> dict:
    """读取 DeepSeek V4 可选高级参数，未配置时保持基础请求形状。"""
    options = {}

    thinking = os.getenv("DONG_THINKING", "").strip().lower()
    if thinking in {"enabled", "disabled"}:
        options["extra_body"] = {"thinking": {"type": thinking}}

    reasoning_effort = os.getenv("DONG_REASONING_EFFORT", "").strip().lower()
    if reasoning_effort in {"high", "max"}:
        options["reasoning_effort"] = reasoning_effort

    response_format = os.getenv("DONG_RESPONSE_FORMAT", "").strip().lower()
    if response_format == "json_object":
        options["response_format"] = {"type": "json_object"}

    return options


def _get_client():
    """懒加载 OpenAI 客户端，避免导入模块时就读取环境或发起初始化。"""
    global _client
    if _client is None:
        base_url = os.getenv("DONG_BASE_URL", "https://api.openai.com/v1")
        _client = OpenAI(
            api_key=os.getenv("DONG_API_KEY") or os.getenv("OPENAI_API_KEY"),
            base_url=base_url,
        )
        log_event(LOGGER, logging.INFO, "llm_client_initialized", base_url=base_url)
    return _client


def get_model():
    """读取当前模型配置；未配置时使用默认模型。"""
    return os.getenv("DONG_MODEL", "gpt-4o")


def chat(messages, tools, model=None):
    """发起一次带工具定义的 LLM 调用，并返回模型消息。"""
    selected_model = model or get_model()
    started = time.monotonic()
    log_event(
        LOGGER,
        logging.INFO,
        "llm_request_started",
        model=selected_model,
        messages=len(messages),
        tools=len(tools),
    )
    try:
        request_options = _deepseek_request_options()
        message = _get_client().chat.completions.create(
            model=selected_model,
            messages=messages,
            tools=tools,
            temperature=0,
            **request_options,
        ).choices[0].message
    except Exception as e:
        duration_ms = int((time.monotonic() - started) * 1000)
        log_event(
            LOGGER,
            logging.ERROR,
            "llm_request_failed",
            model=selected_model,
            duration_ms=duration_ms,
            error=type(e).__name__,
        )
        raise

    duration_ms = int((time.monotonic() - started) * 1000)
    tool_calls = getattr(message, "tool_calls", None) or []
    content = getattr(message, "content", None) or ""
    log_event(
        LOGGER,
        logging.INFO,
        "llm_request_finished",
        model=selected_model,
        duration_ms=duration_ms,
        tool_calls=len(tool_calls),
        content_chars=len(content),
    )
    return message
