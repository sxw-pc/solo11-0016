"""通用类型转换与小型工具函数。"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class ArchiveError(Exception):
    """工具内可预期的业务异常基类。"""


class RuleError(ArchiveError):
    """提取规则或配置不合法。"""


class EncodingError(ArchiveError):
    """文件编码无法识别。"""


class ParseError(ArchiveError):
    """HTML 结构无法解析或未匹配规则。"""


def utc_now() -> str:
    """返回带时区的 ISO 时间戳，便于跨机器排序。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_dir(path: os.PathLike[str] | str) -> Path:
    """创建目录并返回 Path。"""
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def read_json(text: str, default: Any = None) -> Any:
    """安全读取 JSON 字符串。"""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def write_json(path: os.PathLike[str] | str, data: Any) -> Path:
    """以 UTF-8 写入缩进 JSON。"""
    target = Path(path)
    ensure_dir(target.parent)
    target.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """尽量转换为整数。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """尽量转换为浮点数，支持千分位。"""
    if value is None:
        return default
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def normalize_space(value: str) -> str:
    """折叠连续空白。"""
    return re.sub(r"\s+", " ", value).strip()


def _line_of(text: str, needle: str) -> int:
    """在原文中定位文本首次出现行号，找不到时返回 1。"""
    if not needle:
        return 1
    index = text.find(needle)
    if index < 0:
        return 1
    return text.count("\n", 0, index) + 1


def find_source_line(raw_text: str, needle: str) -> int:
    """公开的行号定位助手，供溯源模块复用。"""
    return _line_of(raw_text, needle)


def truncate(value: Any, length: int = 120) -> str:
    """截断文本用于日志与界面展示。"""
    text = "" if value is None else str(value)
    return text if len(text) <= length else text[: length - 1] + "…"


def human_size(num: int) -> str:
    """将字节数转换为可读单位。"""
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"
