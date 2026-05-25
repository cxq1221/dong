"""日志规范 Module：集中配置 dong 的文件日志和结构化事件写法。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

FALSE_VALUES = {"0", "false", "no", "off"}
DEFAULT_LOG_DIRNAME = "logs"
DEFAULT_LOG_FILENAME = "dong.log"
DEFAULT_LOG_LEVEL = "INFO"
PAYLOAD_PREVIEW_LIMIT = 300

_configured = False
_log_path: Path | None = None


def _has_null_handler(logger: logging.Logger) -> bool:
    """判断 logger 是否已经有兜底 NullHandler。"""
    return any(isinstance(handler, logging.NullHandler) for handler in logger.handlers)


def _ensure_null_handler(logger: logging.Logger) -> None:
    """为未配置文件日志的 logger 添加一次 NullHandler。"""
    if not _has_null_handler(logger):
        logger.addHandler(logging.NullHandler())


class _DongRecordFilter(logging.Filter):
    """为 dong 日志补齐统一字段，避免普通日志记录缺少 dong_event 时报错。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """确保每一行日志都有事件名字段。"""
        if not hasattr(record, "dong_event"):
            record.dong_event = "-"
        return True


def _env_enabled(name: str, *, default: bool) -> bool:
    """按统一规则解析布尔环境变量。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in FALSE_VALUES


def logging_enabled() -> bool:
    """返回文件日志是否启用；默认启用，可用 DONG_LOG_ENABLED=0 关闭。"""
    return _env_enabled("DONG_LOG_ENABLED", default=True)


def payload_logging_enabled() -> bool:
    """返回是否允许记录 prompt、命令等正文预览；默认关闭以降低泄露风险。"""
    return _env_enabled("DONG_LOG_PAYLOADS", default=False)


def _log_level() -> int:
    """读取并校验日志级别；非法值回退到 INFO。"""
    raw_level = os.getenv("DONG_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper()
    level = getattr(logging, raw_level, None)
    if isinstance(level, int):
        return level
    return logging.INFO


def _validate_log_path(root: Path, path: Path) -> Path:
    """确保日志路径最终仍在 workdir 内，防止环境变量造成路径穿越。"""
    resolved = path.resolve()
    if resolved != root and not str(resolved).startswith(str(root) + os.sep):
        raise PermissionError(f"Log path traversal denied: {resolved}")
    return resolved


def resolve_log_path(
    workdir: str | os.PathLike[str],
    *,
    file_path: str | os.PathLike[str] | None = None,
) -> Path:
    """根据 workdir、显式路径和环境变量解析最终日志文件路径。"""
    root = Path(workdir).resolve()
    if file_path is not None:
        selected_path = Path(file_path).expanduser()
        if not selected_path.is_absolute():
            selected_path = root / selected_path
        return _validate_log_path(root, selected_path)

    configured_file = os.getenv("DONG_LOG_FILE")
    if configured_file:
        env_file_path = Path(configured_file).expanduser()
        if not env_file_path.is_absolute():
            env_file_path = root / env_file_path
        return _validate_log_path(root, env_file_path)

    configured_dir = os.getenv("DONG_LOG_DIR")
    if configured_dir:
        log_dir = Path(configured_dir).expanduser()
        if not log_dir.is_absolute():
            log_dir = root / log_dir
    else:
        log_dir = root / DEFAULT_LOG_DIRNAME
    return _validate_log_path(root, log_dir / DEFAULT_LOG_FILENAME)


def configure_logging(
    workdir: str | os.PathLike[str],
    *,
    force: bool = False,
) -> Path | None:
    """初始化 dong 文件日志，默认写入 `<workdir>/logs/dong.log`。"""
    global _configured, _log_path

    logger = logging.getLogger("dong")
    logger.addFilter(_DongRecordFilter())

    if not logging_enabled():
        for handler in list(logger.handlers):
            if getattr(handler, "_dong_handler", False):
                logger.removeHandler(handler)
                handler.close()
        _ensure_null_handler(logger)
        _configured = True
        _log_path = None
        return None

    path = resolve_log_path(workdir)
    if _configured and not force and _log_path == path:
        logger.setLevel(_log_level())
        return _log_path

    for handler in list(logger.handlers):
        if getattr(handler, "_dong_handler", False):
            logger.removeHandler(handler)
            handler.close()

    path.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.FileHandler(path, encoding="utf-8")
    handler._dong_handler = True  # type: ignore[attr-defined]
    handler.addFilter(_DongRecordFilter())
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s pid=%(process)d %(name)s "
        "event=%(dong_event)s %(message)s",
    ))

    logger.setLevel(_log_level())
    logger.propagate = False
    logger.addHandler(handler)
    _configured = True
    _log_path = path

    log_event(
        logger,
        logging.INFO,
        "logging_configured",
        path=str(path),
        log_level=logging.getLevelName(logger.level),
        payloads=payload_logging_enabled(),
    )
    return path


def get_logger(name: str) -> logging.Logger:
    """返回 dong 命名空间下的 logger，供各 Module 复用。"""
    if name == "dong" or name.startswith("dong."):
        logger_name = name
    else:
        logger_name = f"dong.{name}"
    logger = logging.getLogger(logger_name)
    logger.addFilter(_DongRecordFilter())
    _ensure_null_handler(logger)
    return logger


def preview_payload(value: Any, *, limit: int = PAYLOAD_PREVIEW_LIMIT) -> str:
    """在显式启用 payload 日志时，生成长度受控的正文预览。"""
    if not payload_logging_enabled():
        return "<disabled>"
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"...({len(text)} chars)"


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: Any,
) -> None:
    """按统一格式写一条结构化事件日志。"""
    if not logging_enabled():
        return
    message = "fields=" + json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    logger.log(level, message, extra={"dong_event": event})
