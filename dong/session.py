"""dong session：负责把本地对话上下文保存成可恢复的工作区会话。"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from typing import Any

from dong.logging_config import get_logger, log_event
from dong.tools import _validate_path

LOGGER = get_logger(__name__)

SESSION_RELPATH = ".dong/sessions"
SESSION_SUFFIX = ".jsonl"
SESSION_VERSION = 1
MAX_JSON_FIELD_CHARS = 100_000
_SESSION_COUNTER = count(1)
_SECRET_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "password",
    "secret",
    "token",
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|password|secret|token)([\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]+)"
)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[a-z0-9._\-]+")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class SessionError(RuntimeError):
    """session 读写失败的基础异常，供 CLI 统一转成用户可见错误。"""


class SessionNotFound(SessionError):
    """请求的 session 引用无法在当前工作区找到。"""


class SessionPersistenceError(SessionError):
    """session 已更新内存但写盘失败时抛出，调用方应停止继续使用脏状态。"""


@dataclass(frozen=True)
class SessionSummary:
    """session 列表摘要；不加载完整 messages 也能展示和选择恢复目标。"""

    session_id: str
    path: Path
    created_at_ms: int
    updated_at_ms: int
    message_count: int
    workspace_root: str
    model: str | None = None


@dataclass
class Session:
    """单个 dong 会话；messages 是运行时上下文，JSONL 是可恢复快照。"""

    session_id: str
    created_at_ms: int
    updated_at_ms: int
    workspace_root: str
    messages: list[Any] = field(default_factory=list)
    prompt_history: list[dict[str, Any]] = field(default_factory=list)
    compactions: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    persistence_path: Path | None = None

    def append_message(self, message: Any) -> None:
        """追加一条上下文消息；写盘失败时回滚内存，避免恢复出错。"""
        self.messages.append(message)
        self.updated_at_ms = _now_ms()
        try:
            self._append_jsonl_record({
                "type": "message",
                "message": _sanitize_for_json(_message_to_json(message)),
            })
        except Exception as exc:
            self.messages.pop()
            raise SessionPersistenceError(
                f"Failed to append session message: {exc}"
            ) from exc

    def record_prompt(self, text: str) -> None:
        """记录用户入口原文；它不进入模型上下文，只用于恢复后的历史审计。"""
        entry = {"timestamp_ms": _now_ms(), "text": text}
        self.prompt_history.append(entry)
        self.updated_at_ms = entry["timestamp_ms"]
        self._append_jsonl_record({
            "type": "prompt_history",
            **_sanitize_for_json(entry),
        })

    def replace_messages(
        self,
        messages: list[Any],
        *,
        compaction: dict[str, Any] | None = None,
    ) -> None:
        """用压缩后的上下文替换当前消息，并通过 snapshot 固化结果。"""
        self.messages[:] = list(messages)
        self.updated_at_ms = _now_ms()
        if compaction is not None:
            self.compactions.append({
                "timestamp_ms": self.updated_at_ms,
                **compaction,
            })
        self.save_snapshot()

    def save_snapshot(self) -> None:
        """重写完整 JSONL 快照；用于新建、清空和上下文压缩后的状态同步。"""
        path = self._require_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        records = self._snapshot_records()
        payload = "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        )
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        with open(temp_path, "w", encoding="utf-8") as file:
            file.write(payload)
        os.replace(temp_path, path)
        log_event(
            LOGGER,
            logging.INFO,
            "session_snapshot_saved",
            session_id=self.session_id,
            messages=len(self.messages),
            path=str(path),
        )

    def _append_jsonl_record(self, record: dict[str, Any]) -> None:
        """向 JSONL 文件追加一条记录；文件缺失时先写完整快照。"""
        path = self._require_path()
        if not path.exists() or path.stat().st_size == 0:
            self.save_snapshot()
            return
        with open(path, "a", encoding="utf-8") as file:
            for item in (record, self._meta_record()):
                file.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
                file.write("\n")
        log_event(
            LOGGER,
            logging.DEBUG,
            "session_record_appended",
            session_id=self.session_id,
            record_type=record.get("type"),
        )

    def _snapshot_records(self) -> list[dict[str, Any]]:
        """把当前 session 转成 JSONL 记录列表，顺序保持可线性回放。"""
        records: list[dict[str, Any]] = [self._meta_record()]
        records.extend(
            {"type": "compaction", **_sanitize_for_json(item)}
            for item in self.compactions
        )
        records.extend(
            {"type": "prompt_history", **_sanitize_for_json(item)}
            for item in self.prompt_history
        )
        records.extend(
            {"type": "message", "message": _sanitize_for_json(_message_to_json(item))}
            for item in self.messages
        )
        return records

    def _meta_record(self) -> dict[str, Any]:
        """生成 session 元信息记录，恢复时用它校验工作区边界。"""
        return {
            "type": "session_meta",
            "version": SESSION_VERSION,
            "session_id": self.session_id,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "workspace_root": self.workspace_root,
            "model": self.model,
            "message_count": len(self.messages),
        }

    def _require_path(self) -> Path:
        """读取持久化路径；没有路径说明当前对象不能落盘。"""
        if self.persistence_path is None:
            raise SessionPersistenceError("session has no persistence path")
        return self.persistence_path

    @classmethod
    def load_from_path(cls, path: Path) -> "Session":
        """从 JSONL 文件回放 session；未知记录类型会被忽略以便向后兼容。"""
        meta: dict[str, Any] | None = None
        messages: list[Any] = []
        prompt_history: list[dict[str, Any]] = []
        compactions: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise SessionError(
                        f"Invalid session JSONL at {path}:{line_number}"
                    ) from exc
                record_type = record.get("type")
                if record_type == "session_meta":
                    meta = record
                elif record_type == "message":
                    messages.append(record.get("message") or {})
                elif record_type == "prompt_history":
                    prompt_history.append(_record_payload(record))
                elif record_type == "compaction":
                    compactions.append(_record_payload(record))

        if meta is None:
            raise SessionError(f"Session file has no session_meta: {path}")

        session = cls(
            session_id=str(meta["session_id"]),
            created_at_ms=int(meta["created_at_ms"]),
            updated_at_ms=int(meta.get("updated_at_ms") or meta["created_at_ms"]),
            workspace_root=str(meta.get("workspace_root") or ""),
            model=meta.get("model"),
            messages=messages,
            prompt_history=prompt_history,
            compactions=compactions,
            persistence_path=path,
        )
        log_event(
            LOGGER,
            logging.INFO,
            "session_loaded",
            session_id=session.session_id,
            messages=len(session.messages),
            path=str(path),
        )
        return session


class SessionStore:
    """按工作区 hash 管理 session 文件，避免不同项目恢复到同一上下文。"""

    def __init__(self, workdir: str) -> None:
        self.workspace_root = str(Path(workdir).resolve())
        self.workspace_hash = workspace_fingerprint(self.workspace_root)
        root = _validate_path(
            self.workspace_root,
            f"{SESSION_RELPATH}/{self.workspace_hash}",
        )
        self.sessions_root = Path(root)

    def create(self, *, model: str | None = None) -> Session:
        """创建新 session 并立即写入空快照，保证进程崩溃也有可追踪文件。"""
        now = _now_ms()
        session_id = _new_session_id(now)
        session = Session(
            session_id=session_id,
            created_at_ms=now,
            updated_at_ms=now,
            workspace_root=self.workspace_root,
            model=model,
            persistence_path=self._path_for(session_id),
        )
        session.save_snapshot()
        log_event(
            LOGGER,
            logging.INFO,
            "session_created",
            session_id=session.session_id,
            path=str(session.persistence_path),
            workspace_hash=self.workspace_hash,
        )
        return session

    def load(self, reference: str) -> Session:
        """按 latest 或 session id 加载当前工作区内的 session。"""
        path = self.resolve_path(reference)
        session = Session.load_from_path(path)
        if not _same_workspace(session.workspace_root, self.workspace_root):
            raise SessionError(
                f"Session {session.session_id} belongs to {session.workspace_root}, "
                f"not {self.workspace_root}"
            )
        return session

    def resolve_path(self, reference: str) -> Path:
        """把用户传入的 session 引用解析为当前工作区内的 JSONL 文件。"""
        ref = (reference or "latest").strip()
        if ref in {"latest", "last", "recent"}:
            latest = self.latest_summary()
            if latest is None:
                raise SessionNotFound("No sessions found for current workspace")
            return latest.path
        session_id = ref[:-len(SESSION_SUFFIX)] if ref.endswith(SESSION_SUFFIX) else ref
        _validate_session_id(session_id)
        path = self._path_for(session_id)
        if not path.exists():
            raise SessionNotFound(f"Session not found: {reference}")
        return path

    def list_summaries(self) -> list[SessionSummary]:
        """列出当前工作区下的 session 摘要，坏文件会被跳过并写入日志。"""
        if not self.sessions_root.exists():
            return []
        summaries = []
        for path in self.sessions_root.glob(f"*{SESSION_SUFFIX}"):
            try:
                summaries.append(_summary_from_path(path))
            except SessionError as exc:
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "session_summary_skipped",
                    path=str(path),
                    error=str(exc),
                )
        summaries.sort(key=lambda item: (item.updated_at_ms, item.session_id), reverse=True)
        return summaries

    def latest_summary(self) -> SessionSummary | None:
        """返回当前工作区最近更新的 session 摘要。"""
        summaries = self.list_summaries()
        return summaries[0] if summaries else None

    def _path_for(self, session_id: str) -> Path:
        """生成受控 session 文件路径；session id 不允许包含路径分隔符。"""
        _validate_session_id(session_id)
        path = _validate_path(
            self.workspace_root,
            f"{SESSION_RELPATH}/{self.workspace_hash}/{session_id}{SESSION_SUFFIX}",
        )
        return Path(path)


def workspace_fingerprint(workspace_root: str) -> str:
    """用 FNV-1a 64-bit 为规范化工作区路径生成稳定短 hash。"""
    value = 0xCBF29CE484222325
    for byte in str(Path(workspace_root).resolve()).encode("utf-8"):
        value ^= byte
        value = (value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def _summary_from_path(path: Path) -> SessionSummary:
    """只读取 JSONL 元信息生成列表摘要，避免为 list 加载完整上下文。"""
    meta: dict[str, Any] | None = None
    message_count = 0
    with open(path, encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise SessionError(
                    f"Invalid session JSONL at {path}:{line_number}"
                ) from exc
            if record.get("type") == "session_meta":
                meta = record
            elif record.get("type") == "message":
                message_count += 1
    if meta is None:
        raise SessionError(f"Session file has no session_meta: {path}")
    return SessionSummary(
        session_id=str(meta["session_id"]),
        path=path,
        created_at_ms=int(meta["created_at_ms"]),
        updated_at_ms=int(meta.get("updated_at_ms") or meta["created_at_ms"]),
        message_count=message_count,
        workspace_root=str(meta.get("workspace_root") or ""),
        model=meta.get("model"),
    )


def _new_session_id(now_ms: int) -> str:
    """生成和 claw-code 近似的时间序 session id，便于人工识别。"""
    return f"session-{now_ms}-{next(_SESSION_COUNTER)}"


def _now_ms() -> int:
    """返回毫秒级时间戳，作为 session 排序和记录时间。"""
    return int(time.time() * 1000)


def _validate_session_id(session_id: str) -> None:
    """验证 session id 是单个文件名，避免通过 id 穿越目录。"""
    if not session_id or not _SESSION_ID_RE.fullmatch(session_id):
        raise SessionError(f"Invalid session id: {session_id!r}")


def _same_workspace(left: str, right: str) -> bool:
    """比较两个工作区路径是否指向同一规范目录。"""
    return str(Path(left).resolve()) == str(Path(right).resolve())


def _record_payload(record: dict[str, Any]) -> dict[str, Any]:
    """移除 JSONL envelope 字段，留下业务 payload。"""
    return {key: value for key, value in record.items() if key != "type"}


def _message_to_json(message: Any) -> dict[str, Any]:
    """把 SDK/dict 消息归一化为恢复时可直接传给 LLM adapter 的 dict。"""
    if isinstance(message, dict):
        payload = dict(message)
    else:
        payload = {
            "role": getattr(message, "role", ""),
            "content": getattr(message, "content", "") or "",
        }
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            payload["tool_calls"] = [_tool_call_to_json(item) for item in tool_calls]
        reasoning_content = getattr(message, "reasoning_content", None)
        if isinstance(reasoning_content, str) and reasoning_content:
            payload["reasoning_content"] = reasoning_content
        anthropic_blocks = getattr(message, "_anthropic_content_blocks", None)
        if anthropic_blocks:
            payload["_anthropic_content_blocks"] = anthropic_blocks
    if "tool_calls" in payload:
        payload["tool_calls"] = [
            _tool_call_to_json(item)
            for item in (payload.get("tool_calls") or [])
        ]
    return payload


def _tool_call_to_json(tool_call: Any) -> dict[str, Any]:
    """把 SDK/dict tool_call 归一化为 ChatCompletions 兼容形状。"""
    if isinstance(tool_call, dict):
        function = tool_call.get("function") or {}
        return {
            "id": tool_call.get("id", ""),
            "type": tool_call.get("type", "function"),
            "function": {
                "name": function.get("name", ""),
                "arguments": function.get("arguments", "{}"),
            },
        }
    function = getattr(tool_call, "function", None)
    return {
        "id": getattr(tool_call, "id", ""),
        "type": "function",
        "function": {
            "name": getattr(function, "name", ""),
            "arguments": getattr(function, "arguments", "{}"),
        },
    }


def _sanitize_for_json(value: Any, *, key: str = "") -> Any:
    """递归清洗写盘内容：密钥字段脱敏，超长字符串截断。"""
    if _looks_secret_key(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_for_json(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_for_json(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_for_json(item, key=key) for item in value]
    if isinstance(value, str):
        redacted = _redact_secret_text(value)
        if len(redacted) > MAX_JSON_FIELD_CHARS:
            return (
                redacted[:MAX_JSON_FIELD_CHARS]
                + f"...[truncated {len(redacted) - MAX_JSON_FIELD_CHARS} chars]"
            )
        return redacted
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if hasattr(value, "model_dump"):
        return _sanitize_for_json(value.model_dump(exclude_none=True), key=key)
    if hasattr(value, "__dict__"):
        return _sanitize_for_json(vars(value), key=key)
    return str(value)


def _looks_secret_key(key: str) -> bool:
    """按字段名判断是否应整值脱敏。"""
    normalized = key.lower().replace("-", "_")
    return any(marker in normalized for marker in _SECRET_KEY_MARKERS)


def _redact_secret_text(text: str) -> str:
    """对字符串中的常见密钥赋值和 Bearer token 做轻量脱敏。"""
    redacted = _SECRET_ASSIGNMENT_RE.sub(r"\1\2[redacted]", text)
    return _BEARER_RE.sub(r"\1[redacted]", redacted)
