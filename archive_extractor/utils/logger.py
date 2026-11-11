"""轻量结构化日志工具，仅输出到本地控制台或文件。"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional


class JsonFormatter(logging.Formatter):
    """单行 JSON 日志，便于归档排查。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if isinstance(getattr(record, "extra_data", None), dict):
            payload.update(record.extra_data)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def get_logger(
    name: str = "archive_extractor",
    log_file: Optional[Path] = None,
    level: int = logging.INFO,
) -> logging.Logger:
    """创建不重复添加 handler 的本地日志器。"""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    formatter = JsonFormatter()
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def log_event(logger: logging.Logger, level: int, message: str, **data: Any) -> None:
    """写入附带结构化字段的日志。"""
    logger.log(level, message, extra={"extra_data": data})
