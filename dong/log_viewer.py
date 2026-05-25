"""日志查看 Module：解析 dong 日志，并提供本地过滤和输出格式化。"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TextIO

from dong.logging_config import resolve_log_path

LOG_LINE_RE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"(?P<level>[A-Z]+) "
    r"pid=(?P<pid>\d+) "
    r"(?P<logger>\S+) "
    r"event=(?P<event>\S+) "
    r"fields=(?P<fields>.*)$",
)


@dataclass(frozen=True)
class ParsedLogLine:
    """解析后的单行 dong 日志。"""

    time: str
    level: str
    pid: int
    logger: str
    event: str
    fields: dict
    raw: str


@dataclass(frozen=True)
class LogFilter:
    """日志过滤条件；所有非空条件都必须同时匹配。"""

    level: str | None = None
    event: str | None = None
    logger: str | None = None
    contains: str | None = None


def parse_log_line(raw: str) -> ParsedLogLine | None:
    """把 dong 单行日志解析成结构化对象；无法解析时返回 None。"""
    match = LOG_LINE_RE.match(raw.rstrip("\n"))
    if not match:
        return None
    try:
        fields = json.loads(match.group("fields"))
    except json.JSONDecodeError:
        fields = {"raw_fields": match.group("fields")}
    return ParsedLogLine(
        time=match.group("time"),
        level=match.group("level"),
        pid=int(match.group("pid")),
        logger=match.group("logger"),
        event=match.group("event"),
        fields=fields,
        raw=raw.rstrip("\n"),
    )


def _matches_filter(entry: ParsedLogLine | None, raw: str, criteria: LogFilter) -> bool:
    """判断一行日志是否满足过滤条件，无法解析的行只支持 contains。"""
    if criteria.contains and criteria.contains not in raw:
        return False
    if entry is None:
        return not any((criteria.level, criteria.event, criteria.logger))
    if criteria.level and entry.level.upper() != criteria.level.upper():
        return False
    if criteria.event and entry.event != criteria.event:
        return False
    if criteria.logger and entry.logger != criteria.logger:
        return False
    return True


def filter_log_lines(lines: Iterable[str], criteria: LogFilter) -> list[ParsedLogLine | str]:
    """过滤日志行；解析成功返回 ParsedLogLine，原始行返回 str。"""
    matches: list[ParsedLogLine | str] = []
    for line in lines:
        raw = line.rstrip("\n")
        entry = parse_log_line(raw)
        if _matches_filter(entry, raw, criteria):
            matches.append(entry if entry is not None else raw)
    return matches


def read_filtered_logs(
    workdir: str,
    *,
    file_path: str | None = None,
    limit: int = 200,
    criteria: LogFilter | None = None,
) -> tuple[Path, list[ParsedLogLine | str]]:
    """读取本地日志文件，并返回最后 limit 行匹配结果。"""
    path = resolve_log_path(workdir, file_path=file_path)
    if not path.exists():
        return path, []

    # 日志读取也走 workdir 内路径校验；只读文本，避免把文件内容一次性暴露给模型。
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    filtered = filter_log_lines(lines, criteria or LogFilter())
    if limit > 0:
        filtered = filtered[-limit:]
    return path, filtered


def format_log_entry(entry: ParsedLogLine | str, *, json_mode: bool) -> str:
    """把过滤后的日志项格式化成纯文本或 JSON 行。"""
    if isinstance(entry, str):
        if json_mode:
            return json.dumps({"type": "raw", "raw": entry}, ensure_ascii=False)
        return entry

    payload = {
        "type": "log",
        "time": entry.time,
        "level": entry.level,
        "pid": entry.pid,
        "logger": entry.logger,
        "event": entry.event,
        "fields": entry.fields,
    }
    if json_mode:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return (
        f"{entry.time} {entry.level:<7} {entry.logger} "
        f"event={entry.event} fields={json.dumps(entry.fields, ensure_ascii=False, sort_keys=True)}"
    )


def stream_logs(
    workdir: str,
    *,
    file_path: str | None,
    limit: int,
    criteria: LogFilter,
    json_mode: bool,
    follow: bool,
    interval: float,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    """输出过滤后的日志；follow 模式下持续轮询新增内容。"""
    path, entries = read_filtered_logs(
        workdir,
        file_path=file_path,
        limit=limit,
        criteria=criteria,
    )
    if json_mode:
        stdout.write(json.dumps({"type": "meta", "file": str(path)}, ensure_ascii=False) + "\n")
    else:
        stdout.write(f"Log file: {path}\n")
    for entry in entries:
        stdout.write(format_log_entry(entry, json_mode=json_mode) + "\n")
    stdout.flush()

    if not follow:
        return

    cursor = path.stat().st_size if path.exists() else 0
    while True:
        time.sleep(interval)
        if not path.exists():
            continue
        size = path.stat().st_size
        if size < cursor:
            stderr.write("Log cursor reset (file rotated).\n")
            cursor = 0
        if size == cursor:
            continue
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(cursor)
            new_lines = handle.readlines()
            cursor = handle.tell()
        for entry in filter_log_lines(new_lines, criteria):
            stdout.write(format_log_entry(entry, json_mode=json_mode) + "\n")
        stdout.flush()
