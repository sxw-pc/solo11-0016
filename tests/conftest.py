"""测试固件：临时数据库、规则、快照与合成 HTML。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from archive_extractor.config.loader import load_rules
from archive_extractor.core.archiver import Archiver
from archive_extractor.core.deduplicator import Deduplicator
from archive_extractor.core.tracer import Tracer
from archive_extractor.storage.file_store import FileStore
from archive_extractor.storage.sqlite_client import SQLiteClient

ROOT = Path(__file__).resolve().parents[1]
MOCK = ROOT / "archive_extractor" / "mock_data"


@pytest.fixture
def ruleset():
    return load_rules()


@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def db(data_dir):
    client = SQLiteClient(data_dir / "archive.db")
    yield client
    client.close()


@pytest.fixture
def store(data_dir):
    return FileStore(data_dir / "files")


@pytest.fixture
def tracer(db, store):
    return Tracer(db, store)


@pytest.fixture
def archiver(db, store):
    return Archiver(db, store)


@pytest.fixture
def make_task(db):
    def _make(name: str = "测试任务") -> int:
        return db.create_task(name, str(MOCK), "", {"concurrency": 1})
    return _make


@pytest.fixture
def seed_result(db):
    def _seed(task_id, data, rel_path="demo/form.html", mtime=100.0,
              status="valid", level=""):
        return db.store_result({
            "task_id": task_id, "form_type": "declare",
            "rule_id": "declare_form", "rule_version": "1.0.0+test",
            "rule_content_hash": "test", "data": data, "status": status,
            "error_level": level, "source_rel_path": rel_path,
            "source_md5": "a" * 32, "source_mtime": mtime,
            "snapshot_path": "", "version": 1,
        })
    return _seed


@pytest.fixture
def sample_declare_text():
    return (MOCK / "2023企业申报归档/normal/declare_001.html").read_text(
        encoding="utf-8"
    )


@pytest.fixture
def sample_public_text():
    return (MOCK / "2024政务公示归档/normal/public_001.html").read_text(
        encoding="utf-8"
    )


@pytest.fixture
def sample_register_text():
    return (MOCK / "2024政务公示归档/nested/register/register_001.html").read_text(
        encoding="utf-8"
    )


@pytest.fixture
def write_file(tmp_path):
    def _write(name: str, text: str, encoding: str = "utf-8",
               mtime: float | None = None) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path
    return _write


@pytest.fixture
def dedup_env(db, make_task, seed_result):
    task_id = make_task("去重测试")
    return {
        "db": db, "task_id": task_id, "seed": seed_result,
        "dedup": Deduplicator(db),
        "config": {
            "primary_key": ["declare_no"],
            "fuzzy_fields": ["applicant", "project_name"],
            "threshold": 0.97,
        },
    }


def load_manifest() -> dict:
    return json.loads((MOCK / "mock_manifest.json").read_text(encoding="utf-8"))
