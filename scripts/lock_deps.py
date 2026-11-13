"""生成/校验离线依赖锁定清单 requirements.lock 与 offline_wheels/ 目录。

在可联网的准备机上运行::

    python scripts/lock_deps.py          # 解析并生成锁文件 + 下载全部 wheel
    python scripts/lock_deps.py --check  # 只校验锁文件与重算结果逐行一致

解析过程全部在独立临时目录中进行（pip download 只下载、不安装），
不会改动当前环境已安装的任何包。已存在的锁文件会作为版本约束参与
重算，因此增量重算结果与从零重建逐行一致（前提：requirements.txt
未变且 PyPI 上对应版本未被撤下）。
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import deps_common as dc  # noqa: E402

#: 自举 pip 版本（最后一个同时支持 Python 3.9 的 pip），随锁文件固定。
PIP_BOOTSTRAP_VERSION = "26.0.1"

PIP_ENV_OVERRIDES = {
    "PIP_CONFIG_FILE": os.devnull,
    "PIP_NO_INPUT": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PYTHONNOUSERSITE": "1",
}


@dataclass
class WheelMeta:
    """offline_wheels 中一个 wheel 的元数据。"""

    name: str
    version: str
    filename: str
    requires: list[str] = field(default_factory=list)


def log(message: str) -> None:
    print(f"[lock-deps] {message}")


def run_pip(pip_cmd: list[str], args: list[str], what: str) -> None:
    cmd = pip_cmd + args
    env = os.environ.copy()
    env.update(PIP_ENV_OVERRIDES)
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + "\n" + proc.stderr).splitlines()[-15:])
        raise dc.LockError(f"{what} 失败（pip 退出码 {proc.returncode}）：\n{tail}")


def pip_command(pip_wheel: Path) -> list[str]:
    """以 zipapp 方式从 wheel 直接运行 pip，无需安装。"""
    return [sys.executable, str(pip_wheel) + "/pip"]


def download_pip_wheel(version: str, dest: Path, expect_sha256: str | None) -> Path:
    """从 PyPI 下载指定版本 pip wheel 并按 PyPI 摘要与锁文件摘要做双重校验。"""
    url = f"https://pypi.org/pypi/pip/{version}/json"
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = json.load(resp)
    for file_info in data["urls"]:
        if file_info["filename"].endswith(".whl"):
            digest = file_info["digests"]["sha256"]
            target = dest / file_info["filename"]
            with urllib.request.urlopen(file_info["url"], timeout=120) as resp:
                blob = resp.read()
            actual = dc.sha256_file(_write_bytes(target, blob))
            if actual != digest:
                raise dc.LockError(f"pip wheel 下载损坏：{actual} != {digest}")
            if expect_sha256 and actual != expect_sha256:
                raise dc.LockError(
                    f"PyPI 上的 {file_info['filename']} 与锁文件记录哈希不符：\n"
                    f"  锁文件: {expect_sha256}\n  PyPI:  {actual}"
                )
            return target
    raise dc.LockError(f"PyPI 上未找到 pip {version} 的 wheel")


def _write_bytes(path: Path, blob: bytes) -> Path:
    path.write_bytes(blob)
    return path


def ensure_pip_wheel(tmp: Path, lock_entries: list[dc.Entry] | None,
                     wheels_dir: Path) -> tuple[Path, str]:
    """确保自举 pip wheel 可用，返回 (路径, sha256)。

    优先复用 offline_wheels/ 中已锁定并校验哈希的文件；否则联网下载。
    """
    locked = None
    for entry in lock_entries or []:
        if entry.section == dc.BOOTSTRAP_SECTION and dc.normalize(entry.name) == "pip":
            locked = entry
    if locked and locked.version != PIP_BOOTSTRAP_VERSION:
        log(f"自举 pip 版本更新：{locked.version} -> {PIP_BOOTSTRAP_VERSION}")
        locked = None
    filename = locked.filename if locked else f"pip-{PIP_BOOTSTRAP_VERSION}-py3-none-any.whl"
    expect = locked.sha256 if locked else None
    candidate = wheels_dir / filename
    if candidate.exists():
        actual = dc.sha256_file(candidate)
        if expect and actual != expect:
            raise dc.LockError(
                f"offline_wheels/{filename} 哈希与锁文件不符：\n"
                f"  锁文件: {expect}\n  实际:  {actual}\n"
                "文件可能被篡改，请重新生成锁文件或恢复该 wheel。"
            )
        return candidate, actual
    log(f"下载自举 pip {PIP_BOOTSTRAP_VERSION} ...")
    path = download_pip_wheel(PIP_BOOTSTRAP_VERSION, tmp, expect)
    return path, dc.sha256_file(path)


def write_constraints(tmp: Path, lock_entries: list[dc.Entry] | None) -> Path | None:
    """把已有锁文件的运行时依赖写成 pip 约束文件，保证增量重算版本稳定。"""
    runtime = [e for e in (lock_entries or []) if e.section == dc.RUNTIME_SECTION]
    if not runtime:
        return None
    path = tmp / "constraints.txt"
    lines = [e.requirement() for e in sorted(runtime, key=lambda e: e.key)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def scan_wheels(wheels_dir: Path) -> dict[str, WheelMeta]:
    """扫描目录中的 wheel，返回 规范化包名 -> 元数据。"""
    _, _, _, utils = dc.load_packaging()
    result = {}
    for path in sorted(wheels_dir.glob("*.whl")):
        name, version, _, _ = utils.parse_wheel_filename(path.name)
        requires = _read_requires(path)
        result[dc.normalize(str(name))] = WheelMeta(
            name=str(name), version=str(version), filename=path.name,
            requires=requires,
        )
    return result


def _read_requires(wheel: Path) -> list[str]:
    import zipfile
    with zipfile.ZipFile(wheel) as zf:
        meta_names = [n for n in zf.namelist() if n.endswith(".dist-info/METADATA")]
        if not meta_names:
            return []
        text = zf.read(meta_names[0]).decode("utf-8", "replace")
    requires = []
    for line in text.splitlines():
        if line.startswith("Requires-Dist:"):
            requires.append(line.split(":", 1)[1].strip())
        elif line and not line.startswith((" ", "\t")) and ":" not in line:
            break
    return requires


def download_packages(pip_cmd: list[str], wheels_dir: Path, req_file: Path,
                      constraints: Path | None, what: str) -> None:
    args = ["download", "--no-input", "--disable-pip-version-check",
            "--no-cache-dir", "--only-binary=:all:", "-d", str(wheels_dir),
            "-r", str(req_file)]
    if constraints:
        args += ["-c", str(constraints)]
    run_pip(pip_cmd, args, what)


def discover_conditional(meta: dict[str, WheelMeta], direct_reqs, pip_cmd,
                         wheels_dir: Path, constraints: Path | None,
                         tmp: Path) -> dict[str, str | None]:
    """发现 pip 本机解析漏掉的条件依赖（其他 Python 版本/操作系统才需要）。

    返回 规范化包名 -> 锁文件中应记录的环境标记（None 表示无条件）。
    本机解析已覆盖的包标记为 None；仅在某些平台需要的包保留其标记，
    多个来源的标记按「或」合并，被条件包间接引入的包继承上游条件。
    """
    conditions: dict[str, set[str | None]] = {name: {None} for name in meta}
    requested_extras: dict[str, set[str]] = defaultdict(set)
    seen_reqs: dict[str, list] = defaultdict(list)
    for req in direct_reqs:
        requested_extras[dc.normalize(req.name)] |= set(req.extras)
    for round_no in range(1, 21):
        changed = False
        to_download: dict[str, list] = {}

        def consider(req, parent_cond: str | None,
                     parent_extras: set[str]) -> None:
            nonlocal changed
            key = dc.normalize(req.name)
            marker = str(req.marker) if req.marker else None
            if marker is not None and not dc.marker_possible(
                    marker, parent_extras | {""}):
                return
            requested_extras[key] |= set(req.extras)
            seen_reqs[key].append(req)
            if conditions.get(key) == {None}:
                return  # 本机解析已覆盖，保持无条件
            stripped = dc.strip_extra_marker(marker) if marker else None
            contribution = dc.combine_markers_and(parent_cond, stripped)
            slot = conditions.setdefault(key, set())
            if contribution not in slot:
                slot.add(contribution)
                changed = True
            if key not in meta:
                to_download.setdefault(key, []).append(req)

        for req in direct_reqs:  # 直接依赖自身带标记时，本机解析可能漏掉
            consider(req, None, set())
        for pname in sorted(meta):
            parent_cond = dc.combine_markers_or(sorted(conditions[pname],
                                                       key=lambda m: m or ""))
            for req_text in meta[pname].requires:
                consider(dc.parse_requirement(req_text), parent_cond,
                         requested_extras[pname])
        if to_download:
            req_file = tmp / f"discovered-{round_no}.txt"
            lines = []
            for key in sorted(to_download):
                specs = sorted({str(r.specifier) for r in seen_reqs[key]
                                if str(r.specifier)})
                lines.append(to_download[key][0].name + ",".join(specs))
            req_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            log(f"补充下载条件依赖: {', '.join(sorted(to_download))}")
            download_packages(pip_cmd, wheels_dir, req_file, constraints,
                              "下载条件依赖")
            for key, info in scan_wheels(wheels_dir).items():
                if key != "pip":  # 自举工具不属于运行时依赖闭包
                    meta.setdefault(key, info)
            changed = True
        if not changed:
            break
    else:
        raise dc.LockError("条件依赖解析超过 20 轮仍未收敛，请检查依赖环")
    result = {}
    for key, conds in conditions.items():
        result[key] = dc.combine_markers_or(sorted(conds, key=lambda m: m or ""))
    return result


def parse_direct_requirements(path: Path) -> list:
    reqs = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.split(" #", 1)[0].strip()
        if line:
            reqs.append(dc.parse_requirement(line))
    return reqs


def sync_wheels(wheels_dir: Path, entries: list[dc.Entry], tmp_wheels: Path,
                old_entries: list[dc.Entry] | None) -> None:
    """把 offline_wheels/ 同步为锁文件描述的精确内容。"""
    wheels_dir.mkdir(exist_ok=True)
    desired = {e.filename for e in entries}
    for entry in entries:
        dst = wheels_dir / entry.filename
        if not dst.exists() or dc.sha256_file(dst) != entry.sha256:
            shutil.copyfile(tmp_wheels / entry.filename, dst)
    stale = {e.filename for e in (old_entries or [])} - desired
    for filename in sorted(stale):
        target = wheels_dir / filename
        if target.exists():
            target.unlink()
            log(f"移除过期 wheel: {filename}")
    unknown = sorted(p.name for p in wheels_dir.glob("*.whl") if p.name not in desired)
    if unknown:
        log("警告：offline_wheels/ 中存在锁文件未记录的文件（未动）:")
        for name in unknown:
            log(f"  - {name}")


def build_entries(meta: dict[str, WheelMeta], markers: dict[str, str | None],
                  pip_entry: dc.Entry, tmp_wheels: Path) -> list[dc.Entry]:
    entries = [pip_entry]
    for key in sorted(meta):
        if key == "pip":
            continue
        info = meta[key]
        entries.append(dc.Entry(
            name=info.name, version=info.version, filename=info.filename,
            sha256=dc.sha256_file(tmp_wheels / info.filename),
            marker=markers.get(key), section=dc.RUNTIME_SECTION,
        ))
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="只校验锁文件与重算结果是否逐行一致，不写任何文件")
    parser.add_argument("--requirements", type=Path, default=None,
                        help="直接依赖清单，默认 <项目根>/requirements.txt")
    parser.add_argument("--lock", type=Path, default=None,
                        help="锁文件路径，默认 <项目根>/requirements.lock")
    parser.add_argument("--wheels", type=Path, default=None,
                        help="wheel 目录，默认 <项目根>/offline_wheels")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    req_path = args.requirements or root / dc.REQUIREMENTS_FILENAME
    lock_path = args.lock or root / dc.LOCK_FILENAME
    wheels_dir = args.wheels or root / dc.WHEELS_DIRNAME
    if not req_path.exists():
        raise dc.LockError(f"未找到直接依赖清单: {req_path}")

    old_entries = None
    if lock_path.exists():
        old_entries = dc.parse_lock_text(lock_path.read_text(encoding="utf-8"))

    tmp = Path(tempfile.mkdtemp(prefix="ae-lock-"))
    try:
        tmp_wheels = tmp / "wheels"
        tmp_wheels.mkdir()
        pip_wheel, pip_sha256 = ensure_pip_wheel(tmp, old_entries, wheels_dir)
        if not (tmp_wheels / pip_wheel.name).exists():
            shutil.copyfile(pip_wheel, tmp_wheels / pip_wheel.name)
        sys.path.insert(0, str(pip_wheel))
        dc.reset_packaging_cache()
        dc.load_packaging()
        pip_cmd = pip_command(pip_wheel)
        constraints = write_constraints(tmp, old_entries)

        log("解析 requirements.txt 的完整传递闭包（仅下载，不安装）...")
        download_packages(pip_cmd, tmp_wheels, req_path, constraints,
                          "解析依赖")
        meta = scan_wheels(tmp_wheels)
        meta.pop("pip", None)
        direct = parse_direct_requirements(req_path)
        markers = discover_conditional(meta, direct, pip_cmd, tmp_wheels,
                                       constraints, tmp)

        pip_entry = dc.Entry(name="pip", version=PIP_BOOTSTRAP_VERSION,
                             filename=pip_wheel.name, sha256=pip_sha256,
                             section=dc.BOOTSTRAP_SECTION)
        entries = build_entries(meta, markers, pip_entry, tmp_wheels)
        text = dc.emit_lock(entries, dc.env_note())

        if args.check:
            return check_lock(lock_path, text)

        lock_path.write_text(text, encoding="utf-8")
        log(f"已写入 {lock_path.name}（{len(entries) - 1} 个依赖 + 自举 pip）")
        sync_wheels(wheels_dir, entries, tmp_wheels, old_entries)
        log(f"offline_wheels/ 已同步（{len(entries)} 个 wheel）")
        report_changes(old_entries, entries)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_lock(lock_path: Path, regenerated: str) -> int:
    if not lock_path.exists():
        log(f"错误：锁文件不存在: {lock_path}")
        return 1
    current = lock_path.read_text(encoding="utf-8")
    if current == regenerated:
        log("锁文件与重算结果逐行一致")
        return 0
    diff = difflib.unified_diff(current.splitlines(), regenerated.splitlines(),
                                lineterm="", n=1,
                                fromfile=str(lock_path), tofile="重新计算结果")
    log("锁文件与重算结果不一致，差异如下：")
    for line in list(diff)[:60]:
        print(line)
    log("请运行 python scripts/lock_deps.py 重新生成")
    return 1


def report_changes(old_entries: list[dc.Entry] | None,
                   new_entries: list[dc.Entry]) -> None:
    old = {e.key: e for e in (old_entries or [])}
    new = {e.key: e for e in new_entries}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in set(old) & set(new)
                     if (old[k].version, old[k].sha256) != (new[k].version,
                                                            new[k].sha256))
    for key in added:
        log(f"  新增: {new[key].name}=={new[key].version}")
    for key in changed:
        log(f"  变更: {old[key].name} {old[key].version} -> {new[key].version}")
    for key in removed:
        log(f"  移除: {old[key].name}=={old[key].version}")
    if not (added or removed or changed) and old_entries is not None:
        log("依赖集合无变化")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except dc.LockError as exc:
        print(f"[lock-deps] 错误：{exc}", file=sys.stderr)
        sys.exit(2)
