"""本地 HTML 文件编码检测与安全解码。"""
from __future__ import annotations

import codecs
import re
from pathlib import Path
from typing import Union

from archive_extractor.utils.common import EncodingError

PathLike = Union[str, Path]
BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
CANDIDATES = ("utf-8", "gb18030", "gb2312", "big5")


def _text_quality(text: str) -> float:
    """估算解码质量，控制字符和替换符越多分越低。"""
    if not text:
        return 1.0
    bad = sum(1 for char in text if char == "�")
    bad += sum(1 for char in text if ord(char) < 9)
    return 1.0 - bad / max(len(text), 1)


def detect_encoding(raw: bytes) -> tuple[str, str]:
    """返回解码文本与编码名。

    优先识别 BOM，再按 UTF-8、GB18030、GB2312、Big5 顺序严格试解码；
    HTML meta 中声明的编码也会作为候选。全部失败时抛出 EncodingError。
    """
    if not raw:
        return "", "utf-8"
    for bom, name in BOMS:
        if raw.startswith(bom):
            try:
                return raw.decode(name), name
            except UnicodeDecodeError:
                break
    head = raw[:2048].decode("ascii", errors="ignore").lower()
    match = re.search(r"charset\s*=\s*[\"']?([a-z0-9_-]+)", head)
    declared = []
    if match:
        value = match.group(1)
        declared = [value, value.replace("gbk", "gb18030")]
    candidates = list(dict.fromkeys(declared + list(CANDIDATES)))
    fallback = None
    for name in candidates:
        try:
            text = raw.decode(name)
        except (UnicodeDecodeError, LookupError):
            continue
        quality = _text_quality(text)
        if quality >= 0.99:
            return text, name
        if fallback is None or quality > fallback[2]:
            fallback = (text, name, quality)
    if fallback:
        return fallback[0], fallback[1]
    raise EncodingError(
        "文件编码无法识别，已尝试 UTF-8/GBK/GB2312/Big5", reason="decode_failed"
    )


def read_html(path: PathLike, max_bytes: int | None = None) -> tuple[str, str]:
    """读取 HTML 文件并返回 (文本, 编码)。"""
    file_path = Path(path)
    try:
        size = file_path.stat().st_size
        if max_bytes and size > max_bytes:
            raise EncodingError(
                f"文件超过大小上限: {size} > {max_bytes}",
                reason="file_too_large",
            )
        raw = file_path.read_bytes()
    except OSError as exc:
        raise EncodingError(f"文件读取失败: {exc}", reason="file_unreadable") from exc
    return detect_encoding(raw)
