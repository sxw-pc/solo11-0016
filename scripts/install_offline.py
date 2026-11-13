"""按 requirements.lock 从 offline_wheels/ 离线安装全部依赖。

在断网目标机上运行::

    python scripts/install_offline.py            # 预检 + 安装
    python scripts/install_offline.py --check    # 只做安装前一致性预检

安装前先核对锁文件与 wheel 目录：缺 wheel、哈希不符、wheel 不适用于
本机平台都会在安装开始前指名报出并给出补齐方法，不会装到一半才失败。
空环境（连 pip 都没有）也能运行：脚本会用锁文件里的自举 pip wheel
直接引导安装。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import deps_common as dc  # noqa: E402


def log(message: str) -> None:
    print(f"[install] {message}")


def find_pip_command(pip_wheel: Path) -> list[str]:
    """优先用当前环境的 pip；没有则以 zipapp 方式直接用自举 wheel。"""
    probe = subprocess.run([sys.executable, "-m", "pip", "--version"],
                           capture_output=True)
    if probe.returncode == 0:
        return [sys.executable, "-m", "pip"]
    return [sys.executable, str(pip_wheel) + "/pip"]


def preflight(entries: list[dc.Entry], wheels_dir: Path) -> tuple[list, list, list]:
    """检查每个锁定 wheel 是否存在且哈希相符，返回 (缺失, 哈希不符, 多余)。"""
    missing, mismatched = [], []
    for entry in entries:
        path = wheels_dir / entry.filename
        if not path.exists():
            missing.append(entry)
        else:
            actual = dc.sha256_file(path)
            if actual != entry.sha256:
                mismatched.append((entry, actual))
    known = {e.filename for e in entries}
    extra = sorted(p.name for p in wheels_dir.glob("*.whl")
                   if p.name not in known)
    return missing, mismatched, extra


def report_integrity(missing, mismatched, wheels_dir: Path) -> None:
    print("=" * 70)
    print("[错误] 离线 wheel 目录与 requirements.lock 不一致，安装已取消。")
    if missing:
        print(f"\n缺少 wheel 文件（{len(missing)} 个）：")
        for entry in missing:
            print(f"  - {entry.filename}（{entry.name}=={entry.version}）")
    if mismatched:
        print(f"\n哈希不符（{len(mismatched)} 个，文件可能损坏或被替换）：")
        for entry, actual in mismatched:
            print(f"  - {entry.filename}（{entry.name}=={entry.version}）")
            print(f"      锁文件记录 sha256:{entry.sha256}")
            print(f"      实际文件   sha256:{actual}")
    print("\n补齐方法（在可联网的准备机上执行）：")
    print("  python scripts/lock_deps.py")
    print("    —— 重新解析并下载全部 wheel 到 offline_wheels/")
    if missing:
        entry = missing[0]
        print("或单独补齐缺件（以第一个为例）：")
        print(f'  python -m pip download "{entry.name}=={entry.version}" '
              "--no-deps --only-binary=:all: \\")
        print(f"    -d {dc.WHEELS_DIRNAME}")
    print(f"\n补齐后把 {dc.WHEELS_DIRNAME}/ 目录整体拷贝到本机，再重新运行安装。")
    print("=" * 70)


def report_incompatible(bad: list[dc.Entry]) -> None:
    print("=" * 70)
    print("[错误] 以下 wheel 不适用于本机操作系统 / Python 版本，安装已取消：")
    for entry in bad:
        print(f"  - {entry.filename}")
    print("\n离线包必须与目标机的操作系统、CPU 架构和 Python 大版本匹配。")
    print("请在与目标机一致的准备机上重新生成锁文件与 wheel 目录：")
    print("  python scripts/lock_deps.py")
    print("=" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="只做安装前一致性预检，不实际安装")
    parser.add_argument("--lock", type=Path, default=None,
                        help="锁文件路径，默认 <项目根>/requirements.lock")
    parser.add_argument("--wheels", type=Path, default=None,
                        help="wheel 目录，默认 <项目根>/offline_wheels")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    lock_path = args.lock or root / dc.LOCK_FILENAME
    wheels_dir = args.wheels or root / dc.WHEELS_DIRNAME

    if not lock_path.exists():
        print(f"[错误] 未找到锁文件 {lock_path}\n"
              "请先在可联网的准备机上运行 python scripts/lock_deps.py 生成，"
              "并把锁文件与 offline_wheels/ 一起拷贝到本机。")
        return 2
    if not wheels_dir.is_dir():
        print(f"[错误] 未找到离线 wheel 目录 {wheels_dir}\n"
              "请把准备机上生成的 offline_wheels/ 目录拷贝到项目根目录。")
        return 2

    entries = dc.parse_lock_text(lock_path.read_text(encoding="utf-8"))
    bootstrap = [e for e in entries if e.section == dc.BOOTSTRAP_SECTION]
    runtime = [e for e in entries if e.section == dc.RUNTIME_SECTION]
    if not bootstrap:
        print("[错误] 锁文件缺少 [bootstrap] 自举工具节，请重新生成锁文件。")
        return 2

    missing, mismatched, extra = preflight(entries, wheels_dir)
    if missing or mismatched:
        report_integrity(missing, mismatched, wheels_dir)
        return 1
    log(f"预检通过：{len(entries)} 个 wheel 全部存在且哈希一致")
    if extra:
        log(f"提示：{dc.WHEELS_DIRNAME}/ 中有 {len(extra)} 个锁文件未记录的文件"
            "（不影响安装）：")
        for name in extra:
            log(f"  - {name}")

    pip_wheel = wheels_dir / bootstrap[0].filename
    sys.path.insert(0, str(pip_wheel))
    dc.reset_packaging_cache()
    dc.load_packaging()

    applicable = [e for e in runtime
                  if e.marker is None or dc.evaluate_marker(e.marker)]
    skipped = [e for e in runtime
               if e.marker is not None and not dc.evaluate_marker(e.marker)]
    bad = [e for e in applicable if not dc.wheel_compatible(e.filename)]
    if bad:
        report_incompatible(bad)
        return 1
    if skipped:
        log("本机环境不需要以下条件依赖（保留在锁文件中，不安装）：")
        for entry in skipped:
            log(f"  - {entry.name}=={entry.version}（{entry.marker}）")

    if args.check:
        log(f"预检完成：本机将安装 {len(applicable)} 个依赖包")
        return 0

    pip_cmd = find_pip_command(pip_wheel)
    cmd = pip_cmd + [
        "install", "--no-index", f"--find-links={wheels_dir}",
        "--require-hashes", "-r", str(lock_path),
    ]
    env = os.environ.copy()
    env.update({"PIP_CONFIG_FILE": os.devnull, "PIP_NO_INPUT": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONUTF8": "1"})
    log("开始离线安装（仅使用本地 wheel，不访问网络）...")
    proc = subprocess.run(cmd, env=env)
    if proc.returncode != 0:
        print("=" * 70)
        print(f"[错误] pip 安装失败（退出码 {proc.returncode}）。")
        print("上方为 pip 输出。常见原因：Python 版本与离线包不匹配、"
              "目标环境存在冲突的旧版本。")
        print("可运行 python scripts/install_offline.py --check 重新预检，")
        print("或在与目标机一致的准备机上重新生成锁文件与 wheel 目录。")
        print("=" * 70)
        return proc.returncode
    log(f"安装完成：{len(applicable)} 个依赖包已就绪")
    log("接下来可运行：python -m archive_extractor.cli demo --fresh")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except dc.LockError as exc:
        print(f"[错误] 锁文件解析失败：{exc}", file=sys.stderr)
        sys.exit(2)
