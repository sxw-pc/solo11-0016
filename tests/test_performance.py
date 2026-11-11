"""性能基线：1000 个 HTML 全流程不超过 60 秒、峰值内存不超过 512MB。"""
import ctypes
import sys
import time

from archive_extractor.config.loader import DEFAULT_RULE_DIR
from archive_extractor.core.task_runner import TaskRunner


def _windows_peak_bytes() -> int:
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return 0
    return int(counters.PeakWorkingSetSize)


def peak_memory_bytes() -> int:
    """返回当前进程峰值常驻内存，兼容 Windows 与 POSIX。"""
    if sys.platform == "win32":
        return _windows_peak_bytes()
    import resource

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def test_1000_forms_within_budget(tmp_path, db, data_dir, sample_declare_text):
    root = tmp_path / "archive_1000"
    root.mkdir()
    for index in range(1, 1001):
        text = sample_declare_text.replace("0001", f"{index:04d}")
        (root / f"form_{index:04d}.html").write_text(text, encoding="utf-8")

    runner = TaskRunner(db, data_dir / "files", timeout=30)
    started = time.perf_counter()
    task_id = runner.create_task(
        "性能基线", str(root), str(DEFAULT_RULE_DIR), {"concurrency": 4}
    )
    task = runner.run(task_id)
    elapsed = time.perf_counter() - started
    peak = peak_memory_bytes()

    assert task["status"] == "completed"
    assert task["success_count"] == 1000
    assert elapsed < 60, f"1000 个文件耗时 {elapsed:.1f}s，超过 60s 基线"
    assert peak < 512 * 1024 * 1024, f"峰值内存 {peak / 1024 / 1024:.0f}MB 超限"
