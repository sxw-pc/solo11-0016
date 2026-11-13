"""离线依赖锁定机制的测试：锁文件解析/生成、环境标记、安装前预检。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import deps_common as dc  # noqa: E402
import install_offline as installer  # noqa: E402

LOCK_PATH = ROOT / dc.LOCK_FILENAME
WHEELS_DIR = ROOT / dc.WHEELS_DIRNAME


@pytest.fixture(scope="module")
def lock_entries():
    text = LOCK_PATH.read_text(encoding="utf-8")
    return dc.parse_lock_text(text)


class TestCommittedLock:
    """仓库内置锁文件与 wheel 目录的健康检查（离线可跑）。"""

    def test_lock_parses_and_has_bootstrap(self, lock_entries):
        bootstrap = [e for e in lock_entries if e.section == dc.BOOTSTRAP_SECTION]
        runtime = [e for e in lock_entries if e.section == dc.RUNTIME_SECTION]
        assert len(bootstrap) == 1 and bootstrap[0].key == "pip"
        assert len(runtime) > 0
        for entry in lock_entries:
            assert entry.filename.endswith(".whl")
            assert len(entry.sha256) == 64

    def test_direct_requirements_are_locked(self, lock_entries):
        locked = {e.key for e in lock_entries}
        for raw in (ROOT / dc.REQUIREMENTS_FILENAME).read_text(
                encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                name = line.split("==")[0].strip()
                assert dc.normalize(name) in locked, f"直接依赖未锁定: {name}"

    def test_conditional_entries_keep_markers(self, lock_entries):
        marked = {e.key: e.marker for e in lock_entries if e.marker}
        # 条件依赖必须保留系统标记，而不是展开成无条件安装
        assert "colorama" in marked and "Windows" in marked["colorama"]
        assert "exceptiongroup" in marked
        assert "python_version" in marked["exceptiongroup"]

    def test_wheels_match_lock(self, lock_entries):
        missing, mismatched, _ = installer.preflight(lock_entries, WHEELS_DIR)
        assert not missing, f"缺少 wheel: {[e.filename for e in missing]}"
        assert not mismatched, (
            f"哈希不符: {[e.filename for e, _ in mismatched]}"
        )


class TestLockFormat:
    def _roundtrip(self, entries):
        text = dc.emit_lock(entries, "Python 3.11.2 (linux, aarch64)")
        again = dc.emit_lock(dc.parse_lock_text(text),
                             "Python 3.11.2 (linux, aarch64)")
        assert again == text
        return dc.parse_lock_text(text)

    def test_emit_is_deterministic_and_parseable(self):
        entries = [
            dc.Entry("pip", "26.0.1", "pip-26.0.1-py3-none-any.whl", "a" * 64,
                     section=dc.BOOTSTRAP_SECTION),
            dc.Entry("Flask", "3.0.0", "flask-3.0.0-py3-none-any.whl", "b" * 64),
            dc.Entry("colorama", "0.4.6", "colorama-0.4.6-py2.py3-none-any.whl",
                     "c" * 64, marker='platform_system == "Windows"'),
        ]
        parsed = self._roundtrip(entries)
        assert [e.key for e in parsed] == ["pip", "colorama", "flask"]
        assert parsed[1].marker == 'platform_system == "Windows"'
        assert parsed[0].section == dc.BOOTSTRAP_SECTION

    def test_parse_rejects_missing_wheel_comment(self):
        text = "flask==3.0.0 \\\n    --hash=sha256:" + "b" * 64 + "\n"
        with pytest.raises(dc.LockError):
            dc.parse_lock_text(text)


class TestMarkers:
    def test_split_top_level_respects_quotes_and_parens(self):
        expr = 'python_version < "3.11" and platform_system == "Windows and Doors"'
        assert dc.split_top_level(expr, "and") == [
            'python_version < "3.11"', 'platform_system == "Windows and Doors"']
        nested = '(a == "1" and b == "2") and c == "3"'
        assert dc.split_top_level(nested, "and") == [
            '(a == "1" and b == "2")', 'c == "3"']

    def test_strip_extra_marker(self):
        assert dc.strip_extra_marker(
            'python_full_version <= "3.11.0a6" and extra == "toml"'
        ) == 'python_full_version <= "3.11.0a6"'
        assert dc.strip_extra_marker('extra == "toml"') is None
        assert dc.strip_extra_marker('python_version < "3.11"') == \
            'python_version < "3.11"'

    def test_strip_extra_marker_rejects_disjunction(self):
        with pytest.raises(dc.LockError):
            dc.strip_extra_marker('extra == "a" or python_version < "3.9"')

    def test_marker_possible_across_matrix(self):
        assert dc.marker_possible('python_version < "3.11"')
        assert dc.marker_possible('platform_system == "Windows"')
        assert not dc.marker_possible('python_version < "3.8"')
        assert not dc.marker_possible('platform_system == "Plan9"')
        assert dc.marker_possible('extra == "toml"', {"toml"})
        assert not dc.marker_possible('extra == "toml"', set())

    def test_combine_markers(self):
        merged = dc.combine_markers_or(
            ['sys_platform == "win32"', 'platform_system == "Windows"'])
        assert merged == 'platform_system == "Windows" or sys_platform == "win32"'
        assert dc.combine_markers_or([None, 'python_version < "3.11"']) is None
        assert dc.combine_markers_and(None, 'python_version < "3.11"') == \
            'python_version < "3.11"'
        assert dc.combine_markers_and('python_version < "3.11"', None) == \
            'python_version < "3.11"'
        assert dc.combine_markers_and(
            'python_version < "3.11"', 'python_version < "3.13"') == \
            'python_version < "3.11" and python_version < "3.13"'


class TestPreflight:
    def _make_layout(self, tmp_path):
        wheels = tmp_path / "wheels"
        wheels.mkdir()
        good = wheels / "demo-1.0-py3-none-any.whl"
        good.write_bytes(b"fake-wheel")
        entry = dc.Entry("demo", "1.0", good.name, dc.sha256_file(good))
        return wheels, entry

    def test_preflight_ok(self, tmp_path):
        wheels, entry = self._make_layout(tmp_path)
        missing, mismatched, extra = installer.preflight([entry], wheels)
        assert not missing and not mismatched and not extra

    def test_preflight_reports_missing_by_name(self, tmp_path):
        wheels, entry = self._make_layout(tmp_path)
        (wheels / entry.filename).unlink()
        missing, mismatched, _ = installer.preflight([entry], wheels)
        assert [e.filename for e in missing] == [entry.filename]
        assert not mismatched

    def test_preflight_reports_hash_mismatch(self, tmp_path):
        wheels, entry = self._make_layout(tmp_path)
        with open(wheels / entry.filename, "ab") as fh:
            fh.write(b"tampered")
        missing, mismatched = installer.preflight([entry], wheels)[:2]
        assert not missing
        assert len(mismatched) == 1
        found, actual = mismatched[0]
        assert found.filename == entry.filename
        assert actual != entry.sha256

    def test_preflight_lists_unknown_files(self, tmp_path):
        wheels, entry = self._make_layout(tmp_path)
        (wheels / "stray-0.1-py3-none-any.whl").write_bytes(b"x")
        _, _, extra = installer.preflight([entry], wheels)
        assert extra == ["stray-0.1-py3-none-any.whl"]
