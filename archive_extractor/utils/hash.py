"""文件哈希与内容指纹。"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Union

PathLike = Union[str, Path]
CHUNK_SIZE = 1024 * 1024


def file_hash(path: PathLike, algorithm: str = "md5") -> str:
    """流式计算文件哈希，避免大文件占用内存。"""
    digest = hashlib.new(algorithm)
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_md5(path: PathLike) -> str:
    """返回文件 MD5。"""
    return file_hash(path, "md5")


def file_sha256(path: PathLike) -> str:
    """返回文件 SHA256。"""
    return file_hash(path, "sha256")


def text_fingerprint(text: str) -> str:
    """对规范化文本生成内容指纹，用于模糊去重辅助。"""
    normalized = " ".join(text.split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
