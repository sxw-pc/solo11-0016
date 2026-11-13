"""本地目录递归扫描与增量文件清单。"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from archive_extractor.utils.common import ensure_dir
from archive_extractor.utils.hash import file_md5

HTML_SUFFIXES = {".html", ".htm"}
HIDDEN_CACHE_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
TEMP_SUFFIXES = (".tmp", ".swp", ".swo", ".bak", ".crdownload", "~")


@dataclass
class ScanItem:
    """一个待处理 HTML 文件。"""

    rel_path: str
    abs_path: str
    size: int
    mtime: float
    md5: str
    status: str = "new"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["mtime_iso"] = datetime.fromtimestamp(self.mtime).isoformat(
            timespec="seconds"
        )
        return data


def _is_temp(name: str) -> bool:
    lowered = name.lower()
    return (
        name.startswith((".", "~", "#"))
        or lowered in HIDDEN_CACHE_NAMES
        or lowered.endswith(TEMP_SUFFIXES)
    )


class DirectoryScanner:
    """递归发现 HTML 文件，支持排除规则和增量比对。"""

    def __init__(
        self,
        root: Path | str,
        exclude_dirs: Optional[Iterable[str]] = None,
        exclude_suffixes: Optional[Iterable[str]] = None,
        min_size: int = 0,
        max_size: Optional[int] = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.exclude_dirs = {item.lower() for item in (exclude_dirs or [])}
        self.exclude_suffixes = {item.lower() for item in (exclude_suffixes or [])}
        self.min_size = max(0, int(min_size))
        self.max_size = max_size

    def _excluded_dir(self, name: str) -> bool:
        lowered = name.lower()
        return (
            _is_temp(name)
            or lowered in self.exclude_dirs
            or lowered in {"__macosx", "__pycache__", "node_modules"}
        )

    def _accept(self, path: Path) -> bool:
        if _is_temp(path.name):
            return False
        if path.suffix.lower() not in HTML_SUFFIXES:
            return False
        if any(path.name.lower().endswith(item) for item in self.exclude_suffixes):
            return False
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size < self.min_size:
            return False
        if self.max_size is not None and size > self.max_size:
            return False
        return True

    def scan(self) -> list[ScanItem]:
        """扫描目录。单个文件读取失败时跳过并继续。"""
        items, _ = self.scan_with_failures()
        return items

    def scan_with_failures(self) -> tuple[list[ScanItem], list[dict]]:
        """扫描目录并同时收集扫描阶段失败，供隔离台账分类落账。

        返回 (成功清单, 失败清单)。失败项包含 rel_path、stage、reason 与
        message；不改变既有 scan() 的返回契约。
        """
        if not self.root.exists():
            raise FileNotFoundError(f"扫描目录不存在: {self.root}")
        if not self.root.is_dir():
            raise NotADirectoryError(f"扫描路径不是目录: {self.root}")
        items: list[ScanItem] = []
        failures: list[dict] = []
        for current, dirs, files in os.walk(self.root):
            dirs[:] = sorted(
                name for name in dirs if not self._excluded_dir(name)
            )
            for name in sorted(files):
                path = Path(current) / name
                if not self._accept(path):
                    continue
                rel_path = path.relative_to(self.root).as_posix()
                try:
                    stat = path.stat()
                except OSError as exc:
                    failures.append({
                        "rel_path": rel_path, "stage": "scan",
                        "reason": "stat_failed", "message": f"扫描失败: {exc}",
                    })
                    continue
                try:
                    md5 = file_md5(path)
                except OSError as exc:
                    failures.append({
                        "rel_path": rel_path, "stage": "scan",
                        "reason": "hash_failed", "message": f"扫描失败: {exc}",
                    })
                    continue
                items.append(ScanItem(
                    rel_path=rel_path,
                    abs_path=str(path.resolve()),
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    md5=md5,
                ))
        return items, failures

    def export_manifest(self, output: Path | str, items: list[ScanItem]) -> Path:
        """导出 JSON 清单。"""
        target = Path(output)
        ensure_dir(target.parent)
        payload = {
            "root": str(self.root),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "total": len(items),
            "files": [item.to_dict() for item in items],
        }
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target


def diff_scan(
    current: list[ScanItem], previous: list[dict] | dict[str, dict]
) -> list[ScanItem]:
    """标记 new/modified/unchanged/deleted。

    previous 支持清单中的 files 列表或 {rel_path: item} 映射。
    """
    if isinstance(previous, dict):
        old = previous
    else:
        old = {item["rel_path"]: item for item in previous}
    current_paths = set()
    for item in current:
        current_paths.add(item.rel_path)
        previous_item = old.get(item.rel_path)
        if not previous_item:
            item.status = "new"
        elif previous_item.get("md5") == item.md5:
            item.status = "unchanged"
        else:
            item.status = "modified"
    for rel_path in sorted(set(old) - current_paths):
        item = ScanItem(rel_path, "", 0, 0.0, "", status="deleted")
        current.append(item)
    return current
