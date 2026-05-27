"""日志规范测试：覆盖默认落盘、开关和 payload 预览策略。"""

from __future__ import annotations

import logging

from dong.logging_config import (
    configure_logging,
    get_logger,
    log_event,
    preview_payload,
)


def _flush_dong_logs() -> None:
    """刷新 dong 命名空间下的日志 handler，确保测试能读到文件内容。"""
    for handler in logging.getLogger("dong").handlers:
        handler.flush()


def test_configure_logging_writes_structured_events_to_logs_dir(
    tmp_path,
    monkeypatch,
) -> None:
    """默认日志应写入 workdir/logs/dong.log，并包含统一事件字段。"""
    monkeypatch.delenv("DONG_LOG_ENABLED", raising=False)
    monkeypatch.delenv("DONG_LOG_DIR", raising=False)
    monkeypatch.delenv("DONG_LOG_FILE", raising=False)
    monkeypatch.delenv("DONG_LOG_LEVEL", raising=False)
    monkeypatch.delenv("DONG_LOG_PAYLOADS", raising=False)

    log_path = configure_logging(tmp_path, force=True)
    logger = get_logger("test")

    log_event(logger, logging.INFO, "unit_event", answer=42)
    _flush_dong_logs()

    assert log_path == tmp_path / "logs" / "dong.log"
    rendered = log_path.read_text(encoding="utf-8")
    assert "event=logging_configured" in rendered
    assert "event=unit_event" in rendered
    assert '"answer": 42' in rendered


def test_logging_can_be_disabled(tmp_path, monkeypatch) -> None:
    """DONG_LOG_ENABLED=0 应关闭文件落盘。"""
    monkeypatch.setenv("DONG_LOG_ENABLED", "0")

    log_path = configure_logging(tmp_path, force=True)

    assert log_path is None
    assert not (tmp_path / "logs" / "dong.log").exists()


def test_configure_logging_retargets_when_workdir_changes(tmp_path, monkeypatch) -> None:
    """同一进程多次启动 CLI 时，日志路径应跟随新的 workdir。"""
    monkeypatch.delenv("DONG_LOG_ENABLED", raising=False)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    first_log = configure_logging(first, force=True)
    second_log = configure_logging(second)
    logger = get_logger("retarget")

    log_event(logger, logging.INFO, "retarget_event")
    _flush_dong_logs()

    assert first_log == first / "logs" / "dong.log"
    assert second_log == second / "logs" / "dong.log"
    assert "retarget_event" not in first_log.read_text(encoding="utf-8")
    assert "retarget_event" in second_log.read_text(encoding="utf-8")


def test_log_file_must_stay_under_workdir(tmp_path, monkeypatch) -> None:
    """日志文件路径也应限制在 workdir 内，避免环境变量路径穿越。"""
    monkeypatch.delenv("DONG_LOG_ENABLED", raising=False)
    monkeypatch.setenv("DONG_LOG_FILE", "../outside.log")

    try:
        configure_logging(tmp_path, force=True)
    except PermissionError as exc:
        assert "Log path traversal denied" in str(exc)
    else:
        raise AssertionError("expected PermissionError")


def test_payload_preview_requires_explicit_opt_in(monkeypatch) -> None:
    """payload 预览默认隐藏，显式开启后才输出截断正文。"""
    monkeypatch.delenv("DONG_LOG_PAYLOADS", raising=False)
    assert preview_payload("secret command") == "<disabled>"

    monkeypatch.setenv("DONG_LOG_PAYLOADS", "1")
    assert preview_payload("abcdef", limit=3) == "abc...(6 chars)"
    assert preview_payload("token=raw-secret") == "token=[redacted]"
    assert preview_payload("authorization: Bearer rawtoken") == (
        "authorization: Bearer [redacted]"
    )


def test_configure_logging_uses_five_megabyte_rotation(tmp_path, monkeypatch) -> None:
    """文件日志默认超过 5MiB 后应轮转成分文件保存。"""
    monkeypatch.delenv("DONG_LOG_ENABLED", raising=False)
    monkeypatch.delenv("DONG_LOG_MAX_BYTES", raising=False)
    monkeypatch.delenv("DONG_LOG_BACKUP_COUNT", raising=False)

    configure_logging(tmp_path, force=True)
    handlers = [
        handler
        for handler in logging.getLogger("dong").handlers
        if getattr(handler, "_dong_handler", False)
    ]

    assert handlers[0].maxBytes == 5 * 1024 * 1024
    assert handlers[0].backupCount > 0


def test_logging_rotates_into_split_files(tmp_path, monkeypatch) -> None:
    """日志超过配置大小后，应生成同目录分片文件。"""
    monkeypatch.delenv("DONG_LOG_ENABLED", raising=False)
    monkeypatch.setenv("DONG_LOG_MAX_BYTES", "1024")
    monkeypatch.setenv("DONG_LOG_BACKUP_COUNT", "2")
    log_path = configure_logging(tmp_path, force=True)
    logger = get_logger("rotate")

    for index in range(4):
        log_event(logger, logging.INFO, "large_event", index=index, text="x" * 900)
    _flush_dong_logs()

    assert log_path == tmp_path / "logs" / "dong.log"
    assert (tmp_path / "logs" / "dong.log.1").exists()
