"""离线依赖锁定公共库：锁文件格式、哈希与环境标记工具。

本模块被 scripts/lock_deps.py（生成锁文件）与 scripts/install_offline.py
（离线安装前预检）共同使用。只依赖标准库；需要解析 PEP 508 环境标记时，
通过 ``load_packaging()`` 惰性加载 packaging（优先使用 pip 内嵌副本，
调用方会先把校验过哈希的 pip wheel 加入 sys.path）。
"""
from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

LOCK_FILENAME = "requirements.lock"
REQUIREMENTS_FILENAME = "requirements.txt"
WHEELS_DIRNAME = "offline_wheels"
BOOTSTRAP_SECTION = "bootstrap"
RUNTIME_SECTION = "runtime"

#: 项目声明支持的 Python 大版本（见 README「环境要求」），用于判定条件依赖
#: 的环境标记是否可能在目标机上成立。
SUPPORTED_PYTHONS = ("3.9", "3.10", "3.11", "3.12")

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECTION_RE = re.compile(r"^#\s*\[([a-z]+)\]")
_WHEEL_COMMENT_RE = re.compile(r"^#\s*wheel:\s*(\S+)\s*$")
_EXTRA_CLAUSE_RE = re.compile(r"^\(?\s*extra\s*(==|in)\s*[\"'][^\"']+[\"']\s*\)?$")


class LockError(Exception):
    """锁文件解析、生成或一致性检查失败。"""


@dataclass
class Entry:
    """锁文件中的一条依赖记录。"""

    name: str
    version: str
    filename: str
    sha256: str
    marker: str | None = None
    section: str = RUNTIME_SECTION

    @property
    def key(self) -> str:
        return normalize(self.name)

    def requirement(self) -> str:
        text = f"{self.name}=={self.version}"
        if self.marker:
            text += f"; {self.marker}"
        return text


def normalize(name: str) -> str:
    """PEP 503 规范化包名。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 锁文件解析与生成
# ---------------------------------------------------------------------------

def parse_lock_text(text: str) -> list[Entry]:
    """解析锁文件文本，返回依赖条目列表。

    锁文件同时是合法的 pip requirements 文件：每个条目由
    ``名称==版本[; 标记]``、续行的 ``--hash=sha256:...`` 与一行
    ``# wheel: 文件名`` 注释组成；节标题形如 ``# [runtime] ...``。
    """
    entries: list[Entry] = []
    section = RUNTIME_SECTION
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue
        sec = _SECTION_RE.match(line)
        if sec:
            section = sec.group(1)
            continue
        wheel = _WHEEL_COMMENT_RE.match(line)
        if wheel:
            if not entries or entries[-1].filename:
                raise LockError(f"锁文件格式错误：孤立的 wheel 注释 {line!r}")
            entries[-1].filename = wheel.group(1)
            continue
        if line.startswith("#"):
            continue
        logical = line
        while logical.endswith("\\"):
            if i >= len(lines):
                raise LockError("锁文件格式错误：续行符后缺少内容")
            logical = logical[:-1].rstrip() + " " + lines[i].strip()
            i += 1
        entries.append(_parse_requirement_line(logical, section))
    for entry in entries:
        if not entry.filename:
            raise LockError(f"锁文件格式错误：{entry.name} 缺少 # wheel: 文件名注释")
    return entries


def _parse_requirement_line(logical: str, section: str) -> Entry:
    tokens = logical.split()
    hashes = [t[len("--hash="):] for t in tokens if t.startswith("--hash=")]
    req = " ".join(t for t in tokens if not t.startswith("--hash="))
    marker = None
    if ";" in req:
        req, marker = req.split(";", 1)
        marker = marker.strip() or None
    if "==" not in req:
        raise LockError(f"锁文件格式错误：依赖行缺少 == 版本钉定 {logical!r}")
    name, version = req.split("==", 1)
    name, version = name.strip(), version.strip()
    if not name or not version:
        raise LockError(f"锁文件格式错误：无法解析依赖行 {logical!r}")
    sha256 = next((h for h in hashes if _HASH_RE.match(h)), None)
    if sha256 is None:
        raise LockError(f"锁文件格式错误：{name} 缺少 sha256 哈希")
    sha256 = sha256.split(":", 1)[1]
    return Entry(name=name, version=version, filename="", sha256=sha256,
                 marker=marker, section=section)


def emit_lock(entries: list[Entry], env_note: str) -> str:
    """把依赖条目生成为确定性的锁文件文本（同输入必得同输出）。"""
    bootstrap = sorted((e for e in entries if e.section == BOOTSTRAP_SECTION),
                       key=lambda e: e.key)
    runtime = sorted((e for e in entries if e.section == RUNTIME_SECTION),
                     key=lambda e: e.key)
    parts = [_HEADER.format(env_note=env_note)]
    parts.append("# [bootstrap] 自举工具：空环境先用它安装 pip，再安装下方全部依赖")
    parts.extend(_emit_entry(e) for e in bootstrap)
    parts.append("")
    parts.append("# [runtime] 运行与测试依赖（requirements.txt 的完整传递闭包）")
    parts.extend(_emit_entry(e) for e in runtime)
    return "\n".join(parts) + "\n"


def _emit_entry(entry: Entry) -> str:
    return (f"{entry.requirement()} \\\n"
            f"    --hash=sha256:{entry.sha256}\n"
            f"    # wheel: {entry.filename}\n")


_HEADER = """\
# =============================================================================
# requirements.lock —— 档采匠离线依赖锁定清单（自动生成，请勿手工编辑）
#
# 重新生成: python scripts/lock_deps.py
# 一致校验: python scripts/lock_deps.py --check
# 离线安装: python scripts/install_offline.py
#
# 解析环境: {env_note}
# 内容: requirements.txt 直接依赖 + 全部传递依赖，条件依赖保留环境标记
# =============================================================================
"""


def env_note() -> str:
    """记录解析环境（同一台机器上结果固定）。"""
    import platform
    return (f"Python {platform.python_version()} "
            f"({sys.platform}, {platform.machine() or 'unknown'})")


# ---------------------------------------------------------------------------
# 环境标记处理
# ---------------------------------------------------------------------------

_PACKAGING = None


def load_packaging():
    """惰性加载 packaging，优先使用 pip 内嵌副本（调用方负责先备好 pip）。"""
    global _PACKAGING
    if _PACKAGING is None:
        try:
            from pip._vendor.packaging import markers, requirements, tags, utils
        except Exception:
            try:
                from packaging import markers, requirements, tags, utils
            except Exception as exc:  # pragma: no cover - 防御性分支
                raise LockError(
                    "无法导入 packaging：请先确认 pip 自举 wheel 完整，"
                    "或当前环境已安装 packaging"
                ) from exc
        _PACKAGING = (markers, requirements, tags, utils)
    return _PACKAGING


def reset_packaging_cache() -> None:
    """测试用：清除 packaging 缓存，强制重新加载。"""
    global _PACKAGING
    _PACKAGING = None


def parse_requirement(text: str):
    """用 packaging 解析一条 PEP 508 依赖声明。"""
    _, requirements, _, _ = load_packaging()
    return requirements.Requirement(text)


def evaluate_marker(marker: str, env: dict | None = None) -> bool:
    markers, _, _, _ = load_packaging()
    return bool(markers.Marker(marker).evaluate(env))


def normalize_marker(marker: str) -> str:
    markers, _, _, _ = load_packaging()
    return str(markers.Marker(marker))


def combine_markers_or(markers_: list[str]) -> str | None:
    """把多个标记按「或」合并并规范化；任一为空（无条件）则整体无条件。"""
    unique = sorted({m for m in markers_ if m})
    if len(unique) != len(markers_):
        return None
    if not unique:
        return None
    if len(unique) == 1:
        return normalize_marker(unique[0])
    return normalize_marker(" or ".join(f"({m})" for m in unique))


def combine_markers_and(first: str | None, second: str | None) -> str | None:
    """把两个标记按「与」合并并规范化；任一为 None 表示无条件。"""
    if first is None:
        return second
    if second is None:
        return first
    return normalize_marker(f"({first}) and ({second})")


def split_top_level(expr: str, keyword: str) -> list[str]:
    """按顶层 ``and``/``or`` 切分标记表达式（忽略引号与括号内部分）。"""
    parts, buf = [], []
    depth, quote, i = 0, None, 0
    needle = f" {keyword} "
    while i < len(expr):
        ch = expr[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and expr.startswith(needle, i):
            parts.append("".join(buf).strip())
            buf = []
            i += len(needle)
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf).strip())
    return [p for p in parts if p]


def strip_extra_marker(marker: str) -> str | None:
    """移除标记中的 ``extra == ...`` 合取项（extras 由依赖方触发，

    写入锁文件时不能保留，否则 pip 会把它当作永不成立而跳过）。
    返回 None 表示移除后不再有任何条件。
    """
    if "extra" not in marker:
        return marker
    if len(split_top_level(marker, "or")) > 1:
        raise LockError(f"暂不支持含 extra 的析取标记，请人工处理: {marker}")
    kept = []
    for clause in split_top_level(marker, "and"):
        if "extra" not in clause:
            kept.append(clause)
        elif not _EXTRA_CLAUSE_RE.match(clause):
            raise LockError(f"暂不支持复杂的 extra 标记，请人工处理: {marker}")
    return " and ".join(kept) if kept else None


def matrix_envs() -> list[dict]:
    """构造支持矩阵（Python 大版本 × 操作系统）的标记求值环境。"""
    platforms = [
        {"platform_system": "Windows", "sys_platform": "win32", "os_name": "nt"},
        {"platform_system": "Linux", "sys_platform": "linux", "os_name": "posix"},
        {"platform_system": "Darwin", "sys_platform": "darwin", "os_name": "posix"},
    ]
    envs = []
    for pyver in SUPPORTED_PYTHONS:
        for plat in platforms:
            envs.append({
                "implementation_name": "cpython",
                "implementation_version": f"{pyver}.0",
                "platform_python_implementation": "CPython",
                "platform_machine": "x86_64",
                "platform_release": "",
                "platform_version": "",
                "python_full_version": f"{pyver}.0",
                "python_version": pyver,
                "extra": "",
                **plat,
            })
    return envs


def marker_possible(marker: str, extras: set[str] | None = None) -> bool:
    """判断标记在支持矩阵（含给定 extras）的任一环境下是否可能成立。"""
    for env in matrix_envs():
        for extra in sorted(extras or {""}):
            candidate = dict(env, extra=extra)
            if evaluate_marker(marker, candidate):
                return True
    return False


def wheel_compatible(filename: str) -> bool:
    """判断 wheel 文件的标签是否兼容当前解释器。"""
    _, _, tags_mod, utils = load_packaging()
    try:
        _, _, _, wheel_tags = utils.parse_wheel_filename(filename)
    except Exception:
        return False
    supported = set(tags_mod.sys_tags())
    return any(tag in supported for tag in wheel_tags)
