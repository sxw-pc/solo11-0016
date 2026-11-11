"""目录扫描与增量清单测试。"""
import json
from pathlib import Path

import pytest

from archive_extractor.core.scanner import DirectoryScanner, diff_scan


def test_scan_recurses_and_filters(tmp_path):
    (tmp_path / "normal").mkdir()
    (tmp_path / "normal" / "a.html").write_text("<html>A</html>", encoding="utf-8")
    (tmp_path / "normal" / "B.HTM").write_text("<html>B</html>", encoding="utf-8")
    (tmp_path / "normal" / ".hidden.html").write_text("x", encoding="utf-8")
    (tmp_path / "normal" / "temp.html.tmp").write_text("x", encoding="utf-8")
    (tmp_path / "normal" / "note.txt").write_text("x", encoding="utf-8")
    (tmp_path / "skip").mkdir()
    (tmp_path / "skip" / "c.html").write_text("<html>C</html>", encoding="utf-8")
    items = DirectoryScanner(tmp_path, exclude_dirs=["skip"]).scan()
    assert [item.rel_path for item in items] == ["normal/B.HTM", "normal/a.html"]
    assert all(len(item.md5) == 32 for item in items)
    assert items[0].to_dict()["mtime_iso"]


def test_scan_empty_and_invalid(tmp_path):
    assert DirectoryScanner(tmp_path).scan() == []
    with pytest.raises(FileNotFoundError):
        DirectoryScanner(tmp_path / "missing").scan()
    file_path = tmp_path / "file.html"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        DirectoryScanner(file_path).scan()


def test_scan_size_and_suffix_exclusions(tmp_path):
    (tmp_path / "small.html").write_text("a", encoding="utf-8")
    (tmp_path / "large.html").write_text("x" * 30, encoding="utf-8")
    (tmp_path / "skipme.html").write_text("x" * 20, encoding="utf-8")
    items = DirectoryScanner(
        tmp_path, min_size=2, max_size=50, exclude_suffixes=["me.html"]
    ).scan()
    assert [item.rel_path for item in items] == ["large.html"]


def test_scan_unreadable_file_skipped(tmp_path, monkeypatch):
    good = tmp_path / "good.html"
    good.write_text("<html>ok</html>", encoding="utf-8")
    bad = tmp_path / "bad.html"
    bad.write_text("<html>bad</html>", encoding="utf-8")

    from archive_extractor.core import scanner

    real_stat = Path.stat

    def fake_stat(self, *args, **kwargs):
        if self.name == "bad.html":
            raise OSError("denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(scanner.Path, "stat", fake_stat)
    items = DirectoryScanner(tmp_path).scan()
    assert [item.rel_path for item in items] == ["good.html"]


def test_manifest_export_and_diff(tmp_path):
    (tmp_path / "a.html").write_text("<html>A</html>", encoding="utf-8")
    (tmp_path / "b.html").write_text("<html>B</html>", encoding="utf-8")
    scanner = DirectoryScanner(tmp_path)
    items = scanner.scan()
    output = tmp_path / "out" / "manifest.json"
    scanner.export_manifest(output, items)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["total"] == 2
    old = payload["files"]
    old[0]["md5"] = "changed"
    (tmp_path / "b.html").unlink()
    (tmp_path / "c.html").write_text("<html>C</html>", encoding="utf-8")
    current = scanner.scan()
    diff_scan(current, old)
    status = {item.rel_path: item.status for item in current}
    assert status == {"a.html": "modified", "c.html": "new", "b.html": "deleted"}
    assert diff_scan(scanner.scan(), {row["rel_path"]: row for row in old})[0].status == "modified"


def test_scanner_ignores_system_cache_dirs(tmp_path):
    (tmp_path / "__MACOSX").mkdir()
    (tmp_path / "__MACOSX" / "x.html").write_text("x", encoding="utf-8")
    (tmp_path / "Thumbs.db").write_text("x", encoding="utf-8")
    (tmp_path / "ok.html").write_text("<html>ok</html>", encoding="utf-8")
    items = DirectoryScanner(tmp_path).scan()
    assert [item.rel_path for item in items] == ["ok.html"]
