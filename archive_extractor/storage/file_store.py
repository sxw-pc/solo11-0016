"""原始文件快照与导出文件目录管理。"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from archive_extractor.utils.common import ensure_dir


class FileStore:
    """所有文件均落在 data_dir 内，禁止路径穿越。"""

    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = ensure_dir(base_dir).resolve()
        self.snapshot_dir = ensure_dir(self.base_dir / "snapshots")
        self.export_dir = ensure_dir(self.base_dir / "exports")

    def snapshot(self, source: Path | str, md5: str, original_name: str = "") -> str:
        """按哈希保存不可变快照，重复哈希不重复写入。"""
        source_path = Path(source)
        name = original_name or source_path.name
        safe_name = "".join(
            char if char.isalnum() or char in "._-" else "_" for char in name
        )[:120]
        target_dir = ensure_dir(self.snapshot_dir / md5[:2])
        target = target_dir / f"{md5}_{safe_name}"
        if not target.exists():
            shutil.copy2(source_path, target)
        return target.relative_to(self.base_dir).as_posix()

    def resolve(self, relative_path: str) -> Path:
        """安全解析存储根内路径。"""
        candidate = (self.base_dir / relative_path).resolve()
        if self.base_dir not in candidate.parents and candidate != self.base_dir:
            raise ValueError("非法存储路径")
        return candidate

    def new_export_path(self, extension: str, prefix: str = "ledger") -> Path:
        """生成唯一导出路径。"""
        safe_prefix = "".join(
            char for char in prefix if char.isalnum() or char in "_-"
        )[:40] or "ledger"
        token = uuid.uuid4().hex[:8]
        return self.export_dir / f"{safe_prefix}_{token}.{extension.lstrip('.')}"

    def write_export(self, path: Path, content: bytes | str) -> Path:
        """写入导出文件。"""
        target = self.resolve(str(path)) if not path.is_absolute() else path
        ensure_dir(target.parent)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
        return target
