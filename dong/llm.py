"""LLM API 封装：把 dong 请求转换为 OpenAI / Anthropic provider 请求。"""
import json
import logging
import os
import pathlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from anthropic import Anthropic
from openai import OpenAI

from dong.logging_config import get_logger, log_event

LOGGER = get_logger(__name__)

def _env_file_candidates() -> list[pathlib.Path]:
    """返回 dong 支持的 .env 位置；用户全局配置用于 uv tool 等安装入口。"""
    return [
        pathlib.Path(__file__).resolve().parent.parent / ".env",
        pathlib.Path.home() / ".dong" / ".env",
    ]


def _load_env_files(paths: list[pathlib.Path] | None = None) -> None:
    """按顺序加载 .env；已存在的进程环境变量不被文件覆盖。"""
    for env_path in paths or _env_file_candidates():
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


_load_env_files()

_openai_client = None
_anthropic_client = None
_resolved_api_mode: str | None = None


@dataclass(frozen=True)
class LlmRequest:
    """发给模型的一次请求；instructions 是 dong 的系统提示词接口。"""
    model: str
    instructions: str
    input: list
    tools: list


@dataclass(frozen=True)
class LlmStreamCallbacks:
    """Provider 无关的流式增量回调；Adapter 只负责把协议 delta 翻译进来。"""
    on_text_delta: Callable[[str], None] | None = None
    on_reasoning_delta: Callable[[str], None] | None = None

    @property
    def enabled(self) -> bool:
        """只要有任一增量消费者，就启用 provider 的流式请求。"""
        return self.on_text_delta is not None or self.on_reasoning_delta is not None

    def text(self, delta: str) -> None:
        """发送 assistant 文本增量；空片段不触发 UI。"""
        if delta and self.on_text_delta is not None:
            self.on_text_delta(delta)

    def reasoning(self, delta: str) -> None:
        """发送 reasoning/thinking 增量；当前 UI 可选择不订阅。"""
        if delta and self.on_reasoning_delta is not None:
            self.on_reasoning_delta(delta)


def _chat_request_options() -> dict:
    """读取 ChatCompletions 可选参数，未配置时保持基础请求形状。"""
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


def _stream_attr(item: Any, name: str, default: Any = None) -> Any:
    """兼容流式 chunk 的 SDK 对象和测试替身 dict。"""
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _stream_delta_reasoning(delta: Any) -> str:
    """读取 DeepSeek/OpenAI 兼容 delta 中的 reasoning_content 增量。"""
    reasoning = _stream_attr(delta, "reasoning_content")
    if reasoning:
        return reasoning
    model_extra = _stream_attr(delta, "model_extra") or {}
    if isinstance(model_extra, dict):
        return model_extra.get("reasoning_content") or ""
    return ""


def _stream_tool_call_deltas(delta: Any) -> list:
    """读取 ChatCompletions 流式 delta 中的 tool_calls 增量。"""
    return _stream_attr(delta, "tool_calls") or []


def _responses_request_options() -> dict:
    """读取 Responses API 可选参数，并转换为 Responses 请求形状。"""
    options = {}

    response_format = os.getenv("DONG_RESPONSE_FORMAT", "").strip().lower()
    if response_format == "json_object":
        options["text"] = {"format": {"type": "json_object"}}

    reasoning_effort = os.getenv("DONG_REASONING_EFFORT", "").strip().lower()
    if reasoning_effort in {"high", "medium", "low"}:
        options["reasoning"] = {"effort": reasoning_effort}

    return options


def _get_openai_client():
    """懒加载 OpenAI 客户端，避免导入模块时就读取环境或发起初始化。"""
    global _openai_client
    if _openai_client is None:
        base_url = os.getenv("DONG_BASE_URL", "https://api.openai.com/v1")
        _openai_client = OpenAI(
            api_key=os.getenv("DONG_API_KEY") or os.getenv("OPENAI_API_KEY"),
            base_url=base_url,
        )
        log_event(LOGGER, logging.INFO, "llm_client_initialized", base_url=base_url)
    return _openai_client


def _get_anthropic_client():
    """懒加载 Anthropic Messages 客户端，支持原生 Claude 和 DeepSeek 兼容端点。"""
    global _anthropic_client
    if _anthropic_client is None:
        base_url = os.getenv(
            "DONG_ANTHROPIC_BASE_URL",
            "https://api.deepseek.com/anthropic",
        )
        _anthropic_client = Anthropic(
            api_key=os.getenv("DONG_ANTHROPIC_API_KEY") or os.getenv("DONG_API_KEY"),
            base_url=base_url,
        )
        log_event(
            LOGGER,
            logging.INFO,
            "llm_anthropic_client_initialized",
            base_url=base_url,
        )
    return _anthropic_client


def get_model_name():
    """读取当前模型配置；未配置时使用默认模型。"""
    return os.getenv("DONG_MODEL", "deepseek-v4-pro")


def _api_mode() -> str:
    """读取 LLM API 模式；Anthropic 配置显式存在时不探测 OpenAI Responses。"""
    mode = os.getenv("DONG_LLM_API", "anthropic").strip().lower()
    if os.getenv("DONG_ANTHROPIC_BASE_URL") and mode == "auto":
        return "anthropic"
    if mode in {"auto", "responses", "chat", "anthropic"}:
        return mode
    return "anthropic"


def _effective_api_mode() -> str:
    """返回本进程已探测出的 API 模式，避免不支持 Responses 时反复试错。"""
    if _api_mode() == "auto" and _resolved_api_mode:
        return _resolved_api_mode
    return _api_mode()


def _responses_tools(tools: list) -> list:
    """把 ChatCompletions function schema 转换成 Responses function schema。"""
    converted = []
    for tool in tools:
        if tool.get("type") != "function" or "function" not in tool:
            converted.append(tool)
            continue
        function = tool["function"]
        response_tool = {
            "type": "function",
            "name": function["name"],
            "description": function.get("description", ""),
            "parameters": function.get("parameters", {}),
        }
        if "strict" in function:
            response_tool["strict"] = function["strict"]
        converted.append(response_tool)
    return converted


def _anthropic_tools(tools: list) -> list:
    """把 OpenAI function schema 转换成 Anthropic Messages tools schema。"""
    converted = []
    for tool in tools:
        if tool.get("type") != "function" or "function" not in tool:
            converted.append(tool)
            continue
        function = tool["function"]
        converted.append({
            "name": function["name"],
            "description": function.get("description", ""),
            "input_schema": function.get("parameters", {}),
        })
    return converted


def _message_role(message: Any) -> str | None:
    """兼容 dict 和 OpenAI SDK 消息对象，读取 role。"""
    if isinstance(message, dict):
        return message.get("role")
    return getattr(message, "role", None)


def _message_content(message: Any) -> str:
    """兼容 dict 和 OpenAI SDK 消息对象，读取 content。"""
    if isinstance(message, dict):
        return message.get("content") or ""
    return getattr(message, "content", None) or ""


def _message_reasoning_content(message: Any) -> str:
    """读取 DeepSeek thinking 模式要求回传的 reasoning_content。"""
    if isinstance(message, dict):
        return message.get("reasoning_content") or ""
    reasoning = getattr(message, "reasoning_content", None)
    if reasoning:
        return reasoning
    model_extra = getattr(message, "model_extra", None) or {}
    if isinstance(model_extra, dict):
        return model_extra.get("reasoning_content") or ""
    return ""


def _message_tool_call_id(tool_call: Any) -> str:
    """兼容 dict 和 SDK 对象，读取工具调用 id。"""
    if isinstance(tool_call, dict):
        return tool_call.get("id", "")
    return getattr(tool_call, "id", "")


def _message_tool_call_name(tool_call: Any) -> str:
    """兼容 dict 和 SDK 对象，读取工具调用名称。"""
    if isinstance(tool_call, dict):
        function = tool_call.get("function") or {}
        return function.get("name", "")
    return getattr(getattr(tool_call, "function", None), "name", "")


def _message_tool_call_arguments(tool_call: Any) -> str:
    """兼容 dict 和 SDK 对象，读取工具调用参数 JSON 字符串。"""
    if isinstance(tool_call, dict):
        function = tool_call.get("function") or {}
        return function.get("arguments") or "{}"
    return getattr(getattr(tool_call, "function", None), "arguments", None) or "{}"


def _message_tool_calls(message: Any) -> list:
    """兼容 dict 和 SDK 对象，读取 assistant tool_calls。"""
    if isinstance(message, dict):
        return message.get("tool_calls") or []
    return getattr(message, "tool_calls", None) or []


def _responses_input(messages: list) -> list:
    """把 dong 的消息历史转换成 Responses input items。"""
    items = []
    for message in messages:
        role = _message_role(message)
        if role == "tool":
            call_id = message.get("tool_call_id") if isinstance(message, dict) else None
            items.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": _message_content(message),
            })
            continue

        content = _message_content(message)
        tool_calls = _message_tool_calls(message)
        if content:
            response_role = "developer" if role == "system" else role
            items.append({"role": response_role or "user", "content": content})
        for tool_call in tool_calls:
            items.append({
                "type": "function_call",
                "call_id": _message_tool_call_id(tool_call),
                "name": _message_tool_call_name(tool_call),
                "arguments": _message_tool_call_arguments(tool_call),
            })
    return items


def _json_tool_input(arguments: str) -> dict:
    """把 OpenAI 工具参数字符串转换成 Anthropic tool_use.input 对象。"""
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {"arguments": arguments}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _anthropic_message_content(message: Any) -> Any:
    """读取消息 content；字符串保持字符串，结构化 blocks 原样传递。"""
    if isinstance(message, dict):
        return message.get("content") or ""
    return getattr(message, "content", None) or ""


def _anthropic_messages_and_system(messages: list, instructions: str) -> tuple[list, str]:
    """把 dong 历史转换成 Anthropic messages，并把 system role 合并到顶层 system。"""
    converted = []
    system_parts = [instructions] if instructions else []
    index = 0
    while index < len(messages):
        message = messages[index]
        role = _message_role(message)
        if role == "system":
            content = _message_content(message).strip()
            if content:
                system_parts.append(content)
            index += 1
            continue
        if role == "tool":
            tool_results = []
            while index < len(messages) and _message_role(messages[index]) == "tool":
                tool_message = messages[index]
                tool_use_id = (
                    tool_message.get("tool_call_id")
                    if isinstance(tool_message, dict)
                    else getattr(tool_message, "tool_call_id", None)
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": _message_content(tool_message),
                })
                index += 1
            converted.append({"role": "user", "content": tool_results})
            continue
        if role == "assistant":
            preserved_blocks = (
                message.get("_anthropic_content_blocks")
                if isinstance(message, dict)
                else getattr(message, "_anthropic_content_blocks", None)
            )
            if preserved_blocks:
                converted.append({"role": "assistant", "content": preserved_blocks})
                index += 1
                continue
            content_blocks = []
            content = _message_content(message)
            if content:
                content_blocks.append({"type": "text", "text": content})
            for tool_call in _message_tool_calls(message):
                content_blocks.append({
                    "type": "tool_use",
                    "id": _message_tool_call_id(tool_call),
                    "name": _message_tool_call_name(tool_call),
                    "input": _json_tool_input(_message_tool_call_arguments(tool_call)),
                })
            converted.append({"role": "assistant", "content": content_blocks or ""})
            index += 1
            continue
        converted.append({
            "role": role or "user",
            "content": _anthropic_message_content(message),
        })
        index += 1
    return converted, "\n\n".join(system_parts)


def _chat_tool_call_payload(tool_call: Any) -> dict:
    """把 SDK/dict tool_call 统一转成 ChatCompletions 可 JSON 序列化的形状。"""
    return {
        "id": _message_tool_call_id(tool_call),
        "type": "function",
        "function": {
            "name": _message_tool_call_name(tool_call),
            "arguments": _message_tool_call_arguments(tool_call),
        },
    }


def _chat_message_payload(message: Any) -> dict:
    """把历史消息统一转成 ChatCompletions 接口可接受的 dict。"""
    role = _message_role(message) or "user"
    if role == "tool":
        tool_call_id = (
            message.get("tool_call_id")
            if isinstance(message, dict)
            else getattr(message, "tool_call_id", None)
        )
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": _message_content(message),
        }

    payload = {"role": role, "content": _message_content(message)}
    tool_calls = _message_tool_calls(message)
    if role == "assistant" and tool_calls:
        payload["tool_calls"] = [
            _chat_tool_call_payload(tool_call)
            for tool_call in tool_calls
        ]
    if role == "assistant":
        reasoning_content = _message_reasoning_content(message)
        if reasoning_content:
            payload["reasoning_content"] = reasoning_content
    return payload


def _chat_messages(messages: list, instructions: str) -> list:
    """把 instructions 兼容成 ChatCompletions 的 system message，并归一化历史。"""
    converted = [_chat_message_payload(message) for message in messages]
    if not instructions:
        return converted
    return [{"role": "system", "content": instructions}, *converted]


def _is_responses_unsupported(error: Exception) -> bool:
    """判断 provider 是否不支持 Responses API，避免掩盖鉴权或限流错误。"""
    status_code = getattr(error, "status_code", None)
    return status_code in {400, 404, 405}


def _response_attr(item: Any, name: str, default: Any = None) -> Any:
    """兼容 Responses SDK 对象和测试替身 dict。"""
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _response_text(response: Any) -> str:
    """从 Responses API 返回中提取最终文本。"""
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text
    parts = []
    for item in getattr(response, "output", None) or []:
        if _response_attr(item, "type") != "message":
            continue
        for content in _response_attr(item, "content", []) or []:
            text = _response_attr(content, "text")
            if text:
                parts.append(text)
    return "".join(parts)


def _response_tool_calls(response: Any) -> list:
    """从 Responses API 返回中提取 function_call，并伪装成 ChatCompletions 形状。"""
    calls = []
    for item in getattr(response, "output", None) or []:
        if _response_attr(item, "type") != "function_call":
            continue
        calls.append(SimpleNamespace(
            id=_response_attr(item, "call_id") or _response_attr(item, "id"),
            function=SimpleNamespace(
                name=_response_attr(item, "name"),
                arguments=_response_attr(item, "arguments") or "{}",
            ),
        ))
    return calls


def _usage_value(usage: Any, *names: str) -> int:
    """兼容不同 provider 的 usage 字段命名，返回第一个可用整数。"""
    for name in names:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        if isinstance(value, int):
            return value
    return 0


def _normalized_usage(usage: Any) -> SimpleNamespace | None:
    """把 OpenAI/Anthropic usage 规整成 dong 内部一致字段。"""
    if usage is None:
        return None
    input_tokens = _usage_value(usage, "input_tokens", "prompt_tokens")
    output_tokens = _usage_value(usage, "output_tokens", "completion_tokens")
    cache_creation_input_tokens = _usage_value(usage, "cache_creation_input_tokens")
    cache_read_input_tokens = _usage_value(usage, "cache_read_input_tokens")
    total_tokens = _usage_value(usage, "total_tokens") or (
        input_tokens
        + output_tokens
        + cache_creation_input_tokens
        + cache_read_input_tokens
    )
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        total_tokens=total_tokens,
    )


def _attach_usage(message: Any, usage: Any) -> None:
    """把 provider usage 挂到统一 message 上，保持 chat() 返回形状兼容。"""
    normalized = _normalized_usage(usage)
    if normalized is not None:
        message.usage = normalized


def _responses_message(response: Any) -> SimpleNamespace:
    """把 Responses API 返回转换成 CLI 主循环已经理解的 assistant message。"""
    message = SimpleNamespace(
        role="assistant",
        content=_response_text(response),
        tool_calls=_response_tool_calls(response),
    )
    reasoning_content = getattr(response, "reasoning_content", None)
    if reasoning_content:
        message.reasoning_content = reasoning_content
    _attach_usage(message, getattr(response, "usage", None))
    return message


def _anthropic_block_attr(block: Any, name: str, default: Any = None) -> Any:
    """兼容 Anthropic SDK block 对象和测试替身 dict。"""
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _anthropic_text(response: Any) -> str:
    """从 Anthropic Messages 返回中提取文本 block。"""
    parts = []
    for block in getattr(response, "content", None) or []:
        if _anthropic_block_attr(block, "type") == "text":
            text = _anthropic_block_attr(block, "text")
            if text:
                parts.append(text)
    return "".join(parts)


def _anthropic_content_blocks(response: Any) -> list:
    """保留 Anthropic 原始 content blocks，方便 thinking 模式下一轮原样回传。"""
    blocks = []
    for block in getattr(response, "content", None) or []:
        if isinstance(block, dict):
            blocks.append(dict(block))
            continue
        if hasattr(block, "model_dump"):
            blocks.append(block.model_dump(exclude_none=True))
            continue
        block_type = _anthropic_block_attr(block, "type")
        if block_type == "text":
            blocks.append({"type": "text", "text": _anthropic_block_attr(block, "text", "")})
        elif block_type == "thinking":
            blocks.append({
                "type": "thinking",
                "thinking": _anthropic_block_attr(block, "thinking", ""),
            })
        elif block_type == "tool_use":
            blocks.append({
                "type": "tool_use",
                "id": _anthropic_block_attr(block, "id"),
                "name": _anthropic_block_attr(block, "name"),
                "input": _anthropic_block_attr(block, "input") or {},
            })
    return blocks


def _anthropic_reasoning_content(response: Any) -> str:
    """提取 thinking block；当前不主动启用 thinking，但兼容 provider 返回。"""
    parts = []
    for block in getattr(response, "content", None) or []:
        if _anthropic_block_attr(block, "type") != "thinking":
            continue
        thinking = (
            _anthropic_block_attr(block, "thinking")
            or _anthropic_block_attr(block, "text")
        )
        if thinking:
            parts.append(thinking)
    return "".join(parts)


def _anthropic_tool_calls(response: Any) -> list:
    """从 Anthropic tool_use block 转成 CLI 已消费的 tool_calls 形状。"""
    calls = []
    for block in getattr(response, "content", None) or []:
        if _anthropic_block_attr(block, "type") != "tool_use":
            continue
        tool_input = _anthropic_block_attr(block, "input") or {}
        calls.append(SimpleNamespace(
            id=_anthropic_block_attr(block, "id"),
            function=SimpleNamespace(
                name=_anthropic_block_attr(block, "name"),
                arguments=json.dumps(tool_input, ensure_ascii=False),
            ),
        ))
    return calls


def _anthropic_message(response: Any) -> SimpleNamespace:
    """把 Anthropic Messages 返回转换成 CLI 主循环理解的 assistant message。"""
    message = SimpleNamespace(
        role="assistant",
        content=_anthropic_text(response),
        tool_calls=_anthropic_tool_calls(response),
        _anthropic_content_blocks=_anthropic_content_blocks(response),
    )
    reasoning_content = _anthropic_reasoning_content(response)
    if reasoning_content:
        message.reasoning_content = reasoning_content
    _attach_usage(message, getattr(response, "usage", None))
    return message


def _create_responses(request: LlmRequest):
    """使用 Responses API 发起请求；系统提示词直接放入 instructions。"""
    response = _get_openai_client().responses.create(
        model=request.model,
        instructions=request.instructions,
        input=_responses_input(request.input),
        tools=_responses_tools(request.tools),
        tool_choice="auto",
        temperature=0,
        **_responses_request_options(),
    )
    return _responses_message(response)


def _create_chat_completion(request: LlmRequest):
    """使用 ChatCompletions 发起请求；instructions 兼容为 system message。"""
    response = _get_openai_client().chat.completions.create(
        model=request.model,
        messages=_chat_messages(request.input, request.instructions),
        tools=request.tools,
        temperature=0,
        **_chat_request_options(),
    )
    message = response.choices[0].message
    _attach_usage(message, getattr(response, "usage", None))
    return message


def _create_chat_completion_stream(
    request: LlmRequest,
    *,
    stream_callbacks: LlmStreamCallbacks,
):
    """使用 ChatCompletions stream=True，并累积成 CLI 仍可消费的完整消息。"""
    stream = _get_openai_client().chat.completions.create(
        model=request.model,
        messages=_chat_messages(request.input, request.instructions),
        tools=request.tools,
        temperature=0,
        stream=True,
        **_chat_request_options(),
    )
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_parts: dict[int, dict[str, str]] = {}

    for chunk in stream:
        choices = _stream_attr(chunk, "choices") or []
        if not choices:
            continue
        delta = _stream_attr(choices[0], "delta")
        if delta is None:
            continue

        text_delta = _stream_attr(delta, "content") or ""
        if text_delta:
            content_parts.append(text_delta)
            stream_callbacks.text(text_delta)

        reasoning_delta = _stream_delta_reasoning(delta)
        if reasoning_delta:
            reasoning_parts.append(reasoning_delta)
            stream_callbacks.reasoning(reasoning_delta)

        for tool_call in _stream_tool_call_deltas(delta):
            index = _stream_attr(tool_call, "index", 0) or 0
            current = tool_parts.setdefault(
                index,
                {"id": "", "name": "", "arguments": ""},
            )
            call_id = _stream_attr(tool_call, "id")
            if call_id:
                current["id"] = call_id
            function = _stream_attr(tool_call, "function")
            if function is None:
                continue
            function_name = _stream_attr(function, "name")
            if function_name:
                current["name"] = function_name
            arguments_delta = _stream_attr(function, "arguments") or ""
            if arguments_delta:
                current["arguments"] += arguments_delta

    tool_calls = [
        SimpleNamespace(
            id=part["id"] or f"call_{index}",
            function=SimpleNamespace(
                name=part["name"],
                arguments=part["arguments"] or "{}",
            ),
        )
        for index, part in sorted(tool_parts.items())
        if part["name"]
    ]
    message = SimpleNamespace(
        role="assistant",
        content="".join(content_parts),
        tool_calls=tool_calls,
    )
    reasoning_content = "".join(reasoning_parts)
    if reasoning_content:
        message.reasoning_content = reasoning_content
    return message


def _anthropic_max_tokens() -> int:
    """读取 Anthropic max_tokens；无效配置回退到保守默认值。"""
    raw = os.getenv("DONG_MAX_TOKENS", "").strip()
    if not raw:
        return 4096
    try:
        return max(1, int(raw))
    except ValueError:
        return 4096


def _anthropic_request_kwargs(request: LlmRequest) -> dict:
    """构造 Anthropic Messages 非流式和流式共用的请求参数。"""
    messages, system = _anthropic_messages_and_system(
        request.input,
        request.instructions,
    )
    kwargs = {
        "model": request.model,
        "max_tokens": _anthropic_max_tokens(),
        "system": system,
        "messages": messages,
        "temperature": 0,
    }
    if request.tools:
        kwargs["tools"] = _anthropic_tools(request.tools)
    return kwargs


def _create_anthropic_message(request: LlmRequest):
    """使用 Anthropic Messages API 发起请求，并返回统一 assistant message。"""
    kwargs = _anthropic_request_kwargs(request)
    response = _get_anthropic_client().messages.create(**kwargs)
    return _anthropic_message(response)


def _create_anthropic_message_stream(
    request: LlmRequest,
    *,
    stream_callbacks: LlmStreamCallbacks,
):
    """使用 Anthropic Messages stream，并把最终 Message 转回统一 assistant message。"""
    kwargs = _anthropic_request_kwargs(request)
    with _get_anthropic_client().messages.stream(**kwargs) as stream:
        for event in stream:
            if _stream_attr(event, "type") != "content_block_delta":
                continue
            delta = _stream_attr(event, "delta")
            delta_type = _stream_attr(delta, "type")
            if delta_type == "text_delta":
                stream_callbacks.text(_stream_attr(delta, "text") or "")
            elif delta_type == "thinking_delta":
                stream_callbacks.reasoning(_stream_attr(delta, "thinking") or "")
        return _anthropic_message(stream.get_final_message())


def chat(
    messages,
    tools,
    model=None,
    instructions: str = "",
    *,
    on_text_delta: Callable[[str], None] | None = None,
    on_reasoning_delta: Callable[[str], None] | None = None,
):
    """发起一次带 instructions 和工具定义的 LLM 调用，并返回模型消息。"""
    global _resolved_api_mode
    selected_model = model or get_model_name()
    stream_callbacks = LlmStreamCallbacks(
        on_text_delta=on_text_delta,
        on_reasoning_delta=on_reasoning_delta,
    )
    request = LlmRequest(
        model=selected_model,
        instructions=instructions,
        input=messages,
        tools=tools,
    )
    started = time.monotonic()
    log_event(
        LOGGER,
        logging.INFO,
        "llm_request_started",
        model=selected_model,
        messages=len(messages),
        tools=len(tools),
        instructions_chars=len(instructions),
        api_mode=_effective_api_mode(),
        streaming=stream_callbacks.enabled,
    )
    try:
        mode = _effective_api_mode()
        if mode == "anthropic":
            if stream_callbacks.enabled:
                message = _create_anthropic_message_stream(
                    request,
                    stream_callbacks=stream_callbacks,
                )
            else:
                message = _create_anthropic_message(request)
        elif mode in {"auto", "responses"}:
            try:
                message = _create_responses(request)
                if _api_mode() == "auto":
                    _resolved_api_mode = "responses"
            except Exception as responses_error:
                if mode != "auto" or not _is_responses_unsupported(responses_error):
                    raise
                _resolved_api_mode = "chat"
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "llm_responses_api_fallback",
                    model=selected_model,
                    error=type(responses_error).__name__,
                )
                if stream_callbacks.enabled:
                    message = _create_chat_completion_stream(
                        request,
                        stream_callbacks=stream_callbacks,
                    )
                else:
                    message = _create_chat_completion(request)
        else:
            if stream_callbacks.enabled:
                message = _create_chat_completion_stream(
                    request,
                    stream_callbacks=stream_callbacks,
                )
            else:
                message = _create_chat_completion(request)
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
    usage = getattr(message, "usage", None)
    log_event(
        LOGGER,
        logging.INFO,
        "llm_request_finished",
        model=selected_model,
        duration_ms=duration_ms,
        tool_calls=len(tool_calls),
        content_chars=len(content),
        input_tokens=getattr(usage, "input_tokens", 0),
        output_tokens=getattr(usage, "output_tokens", 0),
        total_tokens=getattr(usage, "total_tokens", 0),
    )
    return message
