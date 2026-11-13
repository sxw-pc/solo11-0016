"""隔离试跑引擎：在独立临时库中复用现有提取链路，绝不接触主库。

设计约束（见 rule_diff 子命令验收要求）：

1. 试跑全程不接收主库连接，使用临时目录内独立 SQLite 文件与独立快照目录，
   正式 results / validations / traces / dedup_reports / 快照目录零写入。
2. 复用 ``DirectoryScanner`` / ``FormExtractor`` / ``validate_data`` /
   ``Deduplicator`` / ``TaskRunner`` 的既有实现，不复制解析与校验逻辑。
3. 正常结束、异常、中断（SIGINT/SIGTERM）都会清理临时目录；
   进程被强杀留下的残余目录由 :func:`sweep_staging_dirs` 在下次启动时清扫。
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import tempfile
import threading
import weakref
from pathlib import Path
from typing import Optional

from archive_extractor.core.task_runner import TaskRunner
from archive_extractor.storage.sqlite_client import SQLiteClient

STAGING_PREFIX = "ae_dryrun_"

# 正在使用的临时目录登记。弱引用即可：引擎对象被回收时登记自动消失，
# 目录本身的删除仍由 close()/atexit 兜底。
_ACTIVE_DIRS: "weakref.WeakValueDictionary[str, _StagingDir]" = (
    weakref.WeakValueDictionary()
)
# 活跃试跑实例的信号注册计数。多个试跑顺序/嵌套执行时共享一套全局
# 处理器，避免后注册者覆盖前者、先结束者恢复出错误处理器。
_SIGNAL_LOCK = threading.Lock()
_SIGNAL_DEPTH = 0
_SAVED_HANDLERS: dict[int, object] = {}


class _StagingDir:
    """一次试跑的临时工作目录，登记后可被信号/退出钩子统一清理。"""

    def __init__(self, base: Optional[Path] = None) -> None:
        parent = Path(base) if base else Path(tempfile.gettempdir())
        parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(
            tempfile.mkdtemp(prefix=f"{STAGING_PREFIX}{os.getpid()}_", dir=parent)
        )
        self.db_path = self.path / "dryrun.db"
        self.files_dir = self.path / "files"
        self.removed = False
        _ACTIVE_DIRS[str(self.path)] = self

    def cleanup(self) -> None:
        if self.removed:
            return
        self.removed = True
        shutil.rmtree(self.path, ignore_errors=True)
        _ACTIVE_DIRS.pop(str(self.path), None)


def sweep_staging_dirs(base: Optional[Path] = None) -> list[str]:
    """清扫历史崩溃残留的试跑临时目录，返回被清理的目录名列表。"""
    parent = Path(base) if base else Path(tempfile.gettempdir())
    cleaned: list[str] = []
    if not parent.is_dir():
        return cleaned
    for child in parent.glob(f"{STAGING_PREFIX}*"):
        if child.is_dir() and str(child) not in _ACTIVE_DIRS:
            shutil.rmtree(child, ignore_errors=True)
            cleaned.append(str(child))
    return cleaned


def _global_signal_handler(signum, frame):  # pragma: no cover - 依赖真实信号
    """中断时清扫全部活跃临时目录，再恢复默认处理器并重新抛出信号。"""
    _cleanup_active_dirs()
    previous = _SAVED_HANDLERS.get(signum)
    signal.signal(signum, previous if previous not in (None,) else signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def _cleanup_active_dirs() -> None:
    for staging in list(_ACTIVE_DIRS.values()):
        staging.cleanup()


atexit.register(_cleanup_active_dirs)


class DryRunInterrupted(RuntimeError):
    """试跑过程中被信号中断。"""


class RuleDryRun:
    """单套规则对同一批 HTML 的一次隔离试跑。"""

    def __init__(
        self,
        ruleset,
        scan_root: Path | str,
        *,
        timeout: float = 10,
        concurrency: int = 1,
        limit: Optional[int] = None,
        staging_base: Optional[Path] = None,
    ) -> None:
        self.ruleset = ruleset
        self.scan_root = Path(scan_root).expanduser().resolve()
        self.timeout = float(timeout)
        self.concurrency = max(1, int(concurrency))
        self.limit = limit
        self._staging = _StagingDir(staging_base)
        self._db: Optional[SQLiteClient] = None
        self._signals_installed = False

    @property
    def staging_path(self) -> Path:
        return self._staging.path

    def _install_signal_handlers(self) -> None:
        global _SIGNAL_DEPTH
        with _SIGNAL_LOCK:
            if _SIGNAL_DEPTH > 0:
                # 已有试跑实例安装了全局处理器，复用即可
                _SIGNAL_DEPTH += 1
                self._signals_installed = True
                return
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    _SAVED_HANDLERS[signum] = signal.signal(
                        signum, _global_signal_handler
                    )
                except (ValueError, OSError):
                    # 非主线程或受限环境下无法注册信号，atexit 与 close 仍兜底
                    continue
            _SIGNAL_DEPTH = 1
            self._signals_installed = True

    def _restore_signal_handlers(self) -> None:
        global _SIGNAL_DEPTH
        if not self._signals_installed:
            return
        self._signals_installed = False
        with _SIGNAL_LOCK:
            _SIGNAL_DEPTH = max(0, _SIGNAL_DEPTH - 1)
            if _SIGNAL_DEPTH > 0:
                return
            for signum, previous in _SAVED_HANDLERS.items():
                try:
                    signal.signal(signum, previous)
                except (ValueError, OSError):  # pragma: no cover
                    continue
            _SAVED_HANDLERS.clear()

    def execute(self) -> dict:
        """执行试跑并从临时库读回结构化台账（内存对象）。"""
        self._install_signal_handlers()
        try:
            self._db = SQLiteClient(self._staging.db_path)
            runner = TaskRunner(
                self._db, self._staging.files_dir, timeout=self.timeout
            )
            task_id = runner.create_task(
                f"rule-diff 隔离试跑 {self.ruleset.signature}",
                str(self.scan_root),
                str(self.ruleset.source_dir),
                {"concurrency": self.concurrency, "timeout": self.timeout},
            )
            runner.run(task_id, limit=self.limit)
            return self._read_ledger(task_id)
        finally:
            self._restore_signal_handlers()

    def _read_ledger(self, task_id: int) -> dict:
        assert self._db is not None
        rows = self._db.query(
            "SELECT * FROM results WHERE task_id = ? ORDER BY source_rel_path, id",
            (task_id,),
        )
        validations = self._db.query(
            """SELECT v.*, r.source_rel_path FROM validations v
               JOIN results r ON r.id = v.result_id
               WHERE r.task_id = ? ORDER BY v.id""",
            (task_id,),
        )
        validation_index: dict[str, list[dict]] = {}
        for item in validations:
            validation_index.setdefault(item["source_rel_path"], []).append({
                "level": item["level"],
                "field": item["field"],
                "message": item["message"],
                "validator": item.get("validator", ""),
            })
        failed_files = self._db.query(
            """SELECT rel_path, md5, error FROM task_files
               WHERE task_id = ? AND status = 'failed' ORDER BY rel_path""",
            (task_id,),
        )
        ledger: dict[str, dict] = {}
        for row in rows:
            ledger[row["source_rel_path"]] = {
                "source_rel_path": row["source_rel_path"],
                "source_md5": row["source_md5"],
                "form_type": row["form_type"],
                "rule_id": row["rule_id"],
                "rule_version": row["rule_version"],
                "rule_content_hash": row["rule_content_hash"],
                "status": row["status"],
                "error_level": row["error_level"],
                "is_deleted": bool(row["is_deleted"]),
                "version": row["version"],
                "data": json.loads(row["data_json"]),
                "validations": validation_index.get(row["source_rel_path"], []),
                "extract_error": None,
            }
        for item in failed_files:
            ledger[item["rel_path"]] = {
                "source_rel_path": item["rel_path"],
                "source_md5": item["md5"],
                "form_type": None,
                "rule_id": None,
                "rule_version": None,
                "rule_content_hash": None,
                "status": "failed",
                "error_level": "error",
                "is_deleted": False,
                "version": 0,
                "data": {},
                "validations": [],
                "extract_error": item["error"],
            }
        task = self._db.get_task(task_id) or {}
        return {
            "task_id": task_id,
            "task_status": task.get("status"),
            "ruleset_signature": self.ruleset.signature,
            "rule_versions": [
                {
                    "rule_id": rule["rule_id"],
                    "version": rule["_effective_version"],
                    "content_hash": rule["_content_hash"],
                }
                for rule in self.ruleset.all()
            ],
            "records": ledger,
        }

    def close(self) -> None:
        """关闭临时连接并删除整个临时目录。可重复调用。"""
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None
        self._staging.cleanup()

    def __enter__(self) -> "RuleDryRun":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
