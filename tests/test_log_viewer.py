"""日志查看测试：覆盖 dong logs 的解析、过滤和 JSON 输出。"""

from __future__ import annotations

import json
import subprocess
import sys

from dong.log_viewer import LogFilter, filter_log_lines, format_log_entry, parse_log_line


def _sample_line(event: str, level: str = "INFO", logger: str = "dong.cli") -> str:
    """生成符合 dong 日志格式的测试行。"""
    return (
        f"2026-05-25 12:00:00,000 {level} pid=123 {logger} "
        f'event={event} fields={{"tool": "read", "success": true}}'
    )


def test_parse_log_line_extracts_event_level_logger_and_fields() -> None:
    """日志解析应提取稳定字段，供过滤和 JSON 输出复用。"""
    entry = parse_log_line(_sample_line("tool_executed", logger="dong.tool"))

    assert entry is not None
    assert entry.level == "INFO"
    assert entry.logger == "dong.tool"
    assert entry.event == "tool_executed"
    assert entry.fields["tool"] == "read"


def test_filter_log_lines_combines_level_event_logger_and_contains() -> None:
    """多个过滤条件应同时生效，避免返回无关日志。"""
    lines = [
        _sample_line("file_read", logger="dong.tools"),
        _sample_line("tool_executed", logger="dong.tool"),
        _sample_line("tool_executed", level="WARNING", logger="dong.tool"),
    ]

    matches = filter_log_lines(
        lines,
        LogFilter(
            level="INFO",
            event="tool_executed",
            logger="dong.tool",
            contains='"tool": "read"',
        ),
    )

    assert len(matches) == 1
    assert getattr(matches[0], "event") == "tool_executed"


def test_format_log_entry_json_outputs_structured_line() -> None:
    """JSON 模式应输出可被机器处理的结构化日志行。"""
    entry = parse_log_line(_sample_line("file_read"))
    assert entry is not None

    payload = json.loads(format_log_entry(entry, json_mode=True))

    assert payload["type"] == "log"
    assert payload["event"] == "file_read"
    assert payload["fields"]["tool"] == "read"


def test_python_directory_logs_entrypoint_filters_by_event(tmp_path) -> None:
    """`python dong logs` 目录入口应能按 event 过滤本地日志。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "dong.log").write_text(
        "\n".join([
            _sample_line("file_read", logger="dong.tools"),
            _sample_line("tool_executed", logger="dong.tool"),
        ]),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "dong",
            "logs",
            "-d",
            str(tmp_path),
            "--event",
            "tool_executed",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "tool_executed" in result.stdout
    assert "file_read" not in result.stdout
