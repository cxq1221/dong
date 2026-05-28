"""session 恢复 Module：集中管理列表展示、上下文接入和恢复摘要。"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Iterable

from dong.session import Session, SessionError, SessionStore

SESSION_TRANSCRIPT_PREVIEW_LIMIT = 12
SESSION_TRANSCRIPT_PREVIEW_CHARS = 500


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


@dataclass(frozen=True)
class SessionTranscriptLine:
    """恢复 session 后展示到 Transcript 的单行上下文摘要。"""

    role: str
    text: str

    def __str__(self) -> str:
        return f"{self.role}: {self.text}"


@dataclass(frozen=True)
class SessionTranscriptPreview:
    """恢复 session 的最近上下文摘要，交给不同 UI Adapter 自行渲染。"""

    lines: tuple[SessionTranscriptLine, ...]

    @property
    def empty(self) -> bool:
        """判断恢复摘要是否没有可展示内容。"""
        return not self.lines

    def __str__(self) -> str:
        """还原为人类可读的摘要文本。"""
        if not self.lines:
            return ""
        return "Recent session content:\n" + "\n".join(
            f"{line.role}: {line.text}" for line in self.lines
        )


@dataclass(frozen=True)
class SessionRestoreResult:
    """选择并恢复 session 后交给 UI 展示的结果对象。"""

    session: Session
    resume_command: str
    transcript_preview: SessionTranscriptPreview


def session_resume_command(workdir: str, session_id: str) -> str:
    """生成可复制的恢复当前 session 命令。"""
    return f"dong -d {shlex.quote(workdir)} --resume {shlex.quote(session_id)}"


def session_list_items(
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
                resume_command=session_resume_command(workdir, summary.session_id),
                current=summary.session_id == current_session_id,
            )
        )
    return items


def restore_session(
    workdir: str,
    session_id: str,
    working: list,
) -> SessionRestoreResult:
    """加载指定 session 并接入当前 REPL working 上下文。"""
    loaded = SessionStore(workdir).load(session_id)
    working[:] = loaded.messages
    loaded.messages = working
    return SessionRestoreResult(
        session=loaded,
        resume_command=session_resume_command(workdir, loaded.session_id),
        transcript_preview=session_transcript_preview(loaded.messages),
    )


def session_transcript_preview(
    messages: Iterable[object],
) -> SessionTranscriptPreview:
    """生成恢复 session 后展示在 UI 里的最近上下文内容。"""
    rows: list[SessionTranscriptLine] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "message")
        text = _message_text(message.get("content"))
        if not text:
            continue
        rows.append(SessionTranscriptLine(role=role, text=text))
    return SessionTranscriptPreview(
        lines=tuple(rows[-SESSION_TRANSCRIPT_PREVIEW_LIMIT:])
    )


def _preview_text(value, *, limit: int = 96) -> str:
    """把消息内容压成单行预览，避免 session 列表占满屏幕。"""
    text = _message_text(value)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _message_text(content) -> str:
    """把 session message content 展开成适合展示的纯文本。"""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        text = " ".join(parts)
    else:
        text = str(content or "")
    text = " ".join(text.split())
    if len(text) > SESSION_TRANSCRIPT_PREVIEW_CHARS:
        return text[: SESSION_TRANSCRIPT_PREVIEW_CHARS - 1] + "…"
    return text
