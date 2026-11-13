"""字段级仲裁：合并策略、溯源档案、整批撤销与跨批冲突测试。"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from openpyxl import load_workbook

from archive_extractor.config.loader import DEFAULT_RULE_DIR, load_rules
from archive_extractor.core.arbiter import (
    ARBITER_VERSION,
    Arbiter,
    ArbitrationError,
    field_rank,
    is_empty,
    logical_key,
)
from archive_extractor.core.task_runner import TaskRunner

MOCK = Path(__file__).resolve().parents[1] / "archive_extractor" / "mock_data"


def _form(declare_no: str, applicant: str, apply_date: str, amount: str = "1000",
          scope: str = "默认申报范围说明") -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><body>
<form id="declareForm" data-form-type="declare">
  <input type="hidden" name="declare_no" value="{declare_no}">
  <input type="hidden" name="credit_code" value="91110000MA00000001">
  <div class="applicant">{applicant}</div>
  <select name="category"><option value="税收优惠" selected></option></select>
  <input type="text" name="contact_name" value="联系人甲">
  <input type="text" name="contact_phone" value="13800000001">
  <input type="date" name="apply_date" value="{apply_date}">
  <div class="project-name">档案数字化项目</div>
  <div class="amount">{amount}</div>
  <input type="text" name="is_urgent" value="否">
  <textarea name="scope">{scope}</textarea>
  <div class="contact-box"><span class="dept">综合部</span>
    <span class="email" data-email="dept@example.test"></span></div>
</form></body></html>"""


@pytest.fixture
def conflict_archive(tmp_path):
    """两份承载同一逻辑表单、字段互相打架的 HTML（非必填字段各有取舍）。

    必填字段两份都齐全（保证候选 valid 以走通去重），冲突放在非必填的
    scope（申报范围）与 amount（申报金额）上：旧文件有范围无金额差异、
    新文件金额不同且范围留空，用于验证非空优先与字段级合并。
    """
    root = tmp_path / "conflict_archive"
    root.mkdir()
    first = root / "a_older.html"
    second = root / "b_newer.html"
    first.write_text(
        _form("SB-2023-9001", "示例申报企业甲", "2023-05-01",
              amount="1000", scope="旧文件独有的申报范围说明"),
        encoding="utf-8",
    )
    second.write_text(
        _form("SB-2023-9001", "示例申报企业甲", "2023-05-01",
              amount="2000", scope=""),
        encoding="utf-8",
    )
    # older 文件修改时间更早，newer 更新。
    import os
    os.utime(first, (1000, 1000))
    os.utime(second, (2000, 2000))
    return root


# --------------------------------------------------------------------------
# 纯决策单元测试
# --------------------------------------------------------------------------
def test_field_rank_non_empty_beats_empty():
    base = {
        "rule_id": "declare_form", "data": {}, "mtime": 100, "order": 0,
        "issues": [], "dedup_config": {"primary_key": ["declare_no"]},
    }
    empty = dict(base, order=0, data={"apply_date": None})
    filled = dict(base, order=1, mtime=50, data={"apply_date": "2023-01-01"})
    score_empty = field_rank(empty, "apply_date")[0]
    score_filled = field_rank(filled, "apply_date")[0]
    # 即使空值文件更新，非空值也应胜出。
    assert score_filled > score_empty
    assert is_empty(None) and is_empty("") and not is_empty("x")


def test_field_rank_validation_level_then_mtime():
    base = {
        "rule_id": "r", "data": {"f": "v"}, "issues": [], "order": 0,
        "dedup_config": {},
    }
    clean = dict(base, mtime=100, issues=[])
    warned = dict(base, mtime=300, issues=[{"field": "f", "level": "warning"}])
    # 有告警的候选即使文件更新，校验等级也使其排在干净候选之后。
    assert field_rank(clean, "f")[0] > field_rank(warned, "f")[0]
    same_level_old = dict(base, mtime=100, order=0)
    same_level_new = dict(base, mtime=200, order=1)
    assert field_rank(same_level_new, "f")[0] > field_rank(same_level_old, "f")[0]


def test_logical_key_uses_primary_key_and_handles_missing():
    candidate = {
        "rule_id": "declare_form",
        "data": {"declare_no": "SB-1", "meta": {"no": "X"}},
        "dedup_config": {"primary_key": ["declare_no"]},
    }
    assert logical_key(candidate) == "SB-1"
    no_pk = dict(candidate, dedup_config={})
    assert logical_key(no_pk) is None
    empty_pk = dict(candidate, data={"declare_no": None},
                    dedup_config={"primary_key": ["declare_no"]})
    assert logical_key(empty_pk) is None


def test_build_decision_merges_field_level_and_keeps_losers(db):
    arbiter = Arbiter(db)
    candidates = [
        {
            "rule_id": "declare_form", "form_type": "declare", "order": 0,
            "rel_path": "old.html", "md5": "a" * 32, "mtime": 100.0,
            "data": {"declare_no": "SB-1", "applicant": "甲", "apply_date": None},
            "traces": [
                {"field": "applicant", "source_line": 7, "selector": ".a",
                 "selector_type": "css"},
            ],
            "issues": [], "status": "valid",
            "dedup_config": {"primary_key": ["declare_no"]},
            "rule_version": "1.0.0+x",
        },
        {
            "rule_id": "declare_form", "form_type": "declare", "order": 1,
            "rel_path": "new.html", "md5": "b" * 32, "mtime": 200.0,
            "data": {"declare_no": "SB-1", "applicant": None,
                     "apply_date": "2023-06-01"},
            "traces": [
                {"field": "apply_date", "source_line": 9, "selector": ".d",
                 "selector_type": "css"},
            ],
            "issues": [], "status": "valid",
            "dedup_config": {"primary_key": ["declare_no"]},
            "rule_version": "1.0.0+x",
        },
    ]
    decision = arbiter.build_decision(candidates)
    assert decision["merged_group_count"] == 1
    group = decision["groups"][0]
    merged = group["merged_data"]
    # 申请人取非空的旧文件，申报日期取非空的新文件——字段级合并。
    assert merged["applicant"] == "甲"
    assert merged["apply_date"] == "2023-06-01"
    adopted = {item["field_path"]: item for item in group["fields"]
               if item["adopted"]}
    assert adopted["applicant"]["source_rel_path"] == "old.html"
    assert adopted["applicant"]["source_line"] == 7
    assert adopted["apply_date"]["source_rel_path"] == "new.html"
    assert adopted["apply_date"]["source_line"] == 9
    losers = [item for item in group["fields"] if not item["adopted"]]
    # 被淘汰值保留来源路径、MD5、行号与规则版本。
    assert {item["source_rel_path"] for item in losers} == {"old.html", "new.html"}
    assert all(item["source_md5"] for item in losers)
    assert all(item["rule_version"] == "1.0.0+x" for item in losers)


# --------------------------------------------------------------------------
# 端到端：任务执行 + 仲裁落库 + 字段溯源
# --------------------------------------------------------------------------
def test_end_to_end_field_merge_and_provenance(
        db, data_dir, conflict_archive):
    runner = TaskRunner(db, data_dir / "files")
    task_id = runner.create_task(
        "冲突任务", str(conflict_archive), str(DEFAULT_RULE_DIR)
    )
    task = runner.run(task_id)
    assert task["status"] == "completed"
    assert task["success_count"] == 2
    batches = db.list_arbitration_batches(task_id)
    assert len(batches) == 1
    batch = batches[0]
    assert batch["arbiter_version"] == ARBITER_VERSION
    assert batch["merged_group_count"] == 1
    # 冠军结果承载合并后的字段值。
    active = db.query(
        """SELECT * FROM results WHERE task_id = ? AND is_deleted = 0""",
        (task_id,),
    )
    assert len(active) == 1
    merged = json.loads(active[0]["data_json"])
    # scope：旧文件非空、新文件空 → 非空优先，采用旧文件。
    assert merged["scope"] == "旧文件独有的申报范围说明"
    # amount：两份都非空 → 校验等级相同，文件修改时间新者胜出。
    assert merged["amount"] == 2000
    # 另一份候选被既有去重逻辑逻辑删除（去重语义不变）。
    deleted = db.query(
        "SELECT * FROM results WHERE task_id = ? AND is_deleted = 1", (task_id,)
    )
    assert len(deleted) == 1 and deleted[0]["status"] == "duplicate"
    # 字段档案里每个字段都能追到实际采用的文件与行号。
    adopted = db.query(
        """SELECT * FROM arbitration_field_records WHERE batch_id = ?
           AND adopted = 1""",
        (batch["id"],),
    )
    by_field = {row["field_path"]: row for row in adopted}
    assert by_field["scope"]["source_rel_path"].endswith("a_older.html")
    assert by_field["scope"]["source_line"] is not None
    assert by_field["amount"]["source_rel_path"].endswith("b_newer.html")
    assert by_field["amount"]["source_line"] is not None
    # 被淘汰值可查，且带来源 MD5/路径/规则版本。
    losers = db.query(
        """SELECT * FROM arbitration_field_records WHERE batch_id = ?
           AND adopted = 0""",
        (batch["id"],),
    )
    assert losers and all(row["source_md5"] for row in losers)
    assert all(row["rule_version"] for row in losers)


def test_each_field_traces_to_file_and_line(db, data_dir, conflict_archive):
    runner = TaskRunner(db, data_dir / "files")
    task_id = runner.create_task(
        "溯源任务", str(conflict_archive), str(DEFAULT_RULE_DIR)
    )
    runner.run(task_id)
    winner = db.query_one(
        """SELECT id FROM results WHERE task_id = ? AND is_deleted = 0""",
        (task_id,),
    )
    provenance = Arbiter(db).field_provenance(winner["id"])
    adopted = [row for row in provenance if row["adopted"]]
    assert adopted
    for row in adopted:
        # 每个采用字段都能追到来源文件、MD5 与规则版本。
        assert row["source_rel_path"]
        assert row["source_md5"]
        assert row["rule_version"]
        # 在 HTML 中有实际取值位置的字段必须带行号；纯默认值字段允许为空。
        if row["selector"]:
            assert row["source_line"] is not None


# --------------------------------------------------------------------------
# 整批撤销：字段/溯源/快照索引/逻辑删除标记一致回退
# --------------------------------------------------------------------------
def test_rollback_restores_pre_batch_state(db, data_dir, conflict_archive):
    runner = TaskRunner(db, data_dir / "files")
    task_id = runner.create_task(
        "撤销任务", str(conflict_archive), str(DEFAULT_RULE_DIR)
    )
    runner.run(task_id)
    batch = db.list_arbitration_batches(task_id)[0]
    before = {
        "results": db.query("SELECT COUNT(*) n FROM results")[0]["n"],
        "traces": db.query("SELECT COUNT(*) n FROM traces")[0]["n"],
        "validations": db.query("SELECT COUNT(*) n FROM validations")[0]["n"],
        "deleted": db.query(
            "SELECT COUNT(*) n FROM results WHERE is_deleted = 1"
        )[0]["n"],
        "files_success": db.query(
            "SELECT COUNT(*) n FROM task_files WHERE status = 'success'"
        )[0]["n"],
    }
    assert before["results"] == 2 and before["deleted"] == 1
    # 去重报告随批次登记，撤销时一并回退。
    assert db.query("SELECT COUNT(*) n FROM dedup_reports")[0]["n"] == 1
    result = runner.rollback_arbitration(batch["batch_no"])
    assert result["status"] == "rolled_back"
    # 批次内新增的结果/溯源/校验全部移除。
    assert db.query("SELECT COUNT(*) n FROM results")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) n FROM traces")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) n FROM validations")[0]["n"] == 0
    # 去重报告与逻辑删除标记一起回到批次前。
    assert db.query("SELECT COUNT(*) n FROM dedup_reports")[0]["n"] == 0
    # 逻辑删除标记与文件状态一起回到批次前（暂存阶段的 processing，
    # 由 reset_processing 在下次运行时复位为 pending，无残留 success）。
    assert db.query(
        "SELECT COUNT(*) n FROM results WHERE is_deleted = 1"
    )[0]["n"] == 0
    assert db.query(
        "SELECT COUNT(*) n FROM task_files WHERE status = 'success'"
    )[0]["n"] == 0
    statuses = {row["status"] for row in db.query(
        "SELECT DISTINCT status FROM task_files"
    )}
    assert statuses <= {"pending", "processing"}
    # 批次标记为已撤销，冲突档案一并清空。
    rolled = db.get_arbitration_batch(batch["id"])
    assert rolled["status"] == "rolled_back"
    assert db.list_arbitration_conflicts(batch["id"]) == []


def test_rollback_twice_and_missing_batch_raise(db, data_dir, conflict_archive):
    runner = TaskRunner(db, data_dir / "files")
    task_id = runner.create_task(
        "重复撤销", str(conflict_archive), str(DEFAULT_RULE_DIR)
    )
    runner.run(task_id)
    batch_no = db.list_arbitration_batches(task_id)[0]["batch_no"]
    runner.rollback_arbitration(batch_no)
    with pytest.raises(ArbitrationError, match="已撤销"):
        runner.rollback_arbitration(batch_no)
    with pytest.raises(ArbitrationError, match="找不到仲裁记录"):
        runner.rollback_arbitration("ARB-NOT-EXIST")
    with pytest.raises(ArbitrationError, match="找不到仲裁记录"):
        Arbiter(db).batch_detail("ARB-NOT-EXIST")


def test_replay_batch_is_consistent(db, data_dir, conflict_archive):
    runner = TaskRunner(db, data_dir / "files")
    task_id = runner.create_task(
        "回放任务", str(conflict_archive), str(DEFAULT_RULE_DIR)
    )
    runner.run(task_id)
    batch_no = db.list_arbitration_batches(task_id)[0]["batch_no"]
    replay = Arbiter(db).replay_batch(batch_no)
    assert replay["consistent"] and not replay["mismatches"]


# --------------------------------------------------------------------------
# 跨批并发冲突：后到批次不静默覆盖，冲突进档案并给可读摘要
# --------------------------------------------------------------------------
def _single_archive(tmp_path, name, declare_no, applicant, mtime):
    import os
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    path = root / "form.html"
    path.write_text(
        _form(declare_no, applicant, "2023-01-01"), encoding="utf-8"
    )
    os.utime(path, (mtime, mtime))
    return root


def test_concurrent_batches_record_conflict_without_silent_overwrite(
        db, data_dir, tmp_path):
    runner = TaskRunner(db, data_dir / "files")
    rules = str(DEFAULT_RULE_DIR)
    root_a = _single_archive(tmp_path, "batch_a", "SB-2023-7001", "企业A", 1000)
    task_a = runner.create_task("批次A", str(root_a), rules)
    runner.run(task_a)
    first_value = json.loads(
        db.query_one(
            "SELECT data_json FROM results WHERE is_deleted = 0"
        )["data_json"]
    )["applicant"]
    assert first_value == "企业A"
    root_b = _single_archive(tmp_path, "batch_b", "SB-2023-7001", "企业B", 2000)
    task_b = runner.create_task("批次B", str(root_b), rules)
    runner.run(task_b)
    # 两个批次都在，第二批的冲突被记录而不是静默抹掉第一批。
    batches = db.list_arbitration_batches()
    assert len(batches) == 2
    second_batch = db.list_arbitration_batches(task_b)[0]
    conflicts = db.list_arbitration_conflicts(second_batch["id"])
    assert conflicts
    conflict = next(c for c in conflicts if c["field_path"] == "applicant")
    assert json.loads(conflict["incumbent_value_json"]) == "企业A"
    assert json.loads(conflict["incoming_value_json"]) == "企业B"
    assert "企业A" in conflict["summary"] and "企业B" in conflict["summary"]
    assert conflict["resolution"] == "recorded_pending_review"
    # 第一批结果仍可查，未被静默物理覆盖。
    assert db.get_task(task_a) is not None


def test_cross_task_conflict_uses_global_identity(db, data_dir, tmp_path):
    """不同 task_id 的两个任务写同一逻辑表单，冲突仍被全局识别并记录。"""
    import os

    def make(root, scope, mtime):
        root.mkdir(parents=True, exist_ok=True)
        path = root / "form.html"
        path.write_text(
            _form("SB-2023-0002", "测试企业", "2023-05-01",
                  amount="1000", scope=scope),
            encoding="utf-8",
        )
        os.utime(path, (mtime, mtime))
        return root

    runner = TaskRunner(db, data_dir / "files")
    rules = str(DEFAULT_RULE_DIR)
    root_a = make(tmp_path / "task_one", "任务一的范围", 1000)
    task_a = runner.create_task("独立任务一", str(root_a), rules)
    runner.run(task_a)
    root_b = make(tmp_path / "task_two", "任务二的范围", 2000)
    task_b = runner.create_task("独立任务二", str(root_b), rules)
    runner.run(task_b)
    second = db.list_arbitration_batches(task_b)[0]
    conflicts = db.list_arbitration_conflicts(second["id"])
    scope_conflict = next(c for c in conflicts if c["field_path"] == "scope")
    assert json.loads(scope_conflict["incumbent_value_json"]) == "任务一的范围"
    assert json.loads(scope_conflict["incoming_value_json"]) == "任务二的范围"
    # 两个任务的结果都保持有效，没有任何一方被静默覆盖。
    assert db.query(
        "SELECT COUNT(*) n FROM results WHERE is_deleted = 0"
    )[0]["n"] == 2


def test_rollback_second_batch_keeps_first_batch_intact(
        db, data_dir, tmp_path):
    """撤销后到的冲突批次：第一批字段、状态与批次档案保持不动。"""
    runner = TaskRunner(db, data_dir / "files")
    rules = str(DEFAULT_RULE_DIR)
    root_a = _single_archive(tmp_path, "ra", "SB-2023-7200", "企业A", 1000)
    task_a = runner.create_task("批次A", str(root_a), rules)
    runner.run(task_a)
    root_b = _single_archive(tmp_path, "rb", "SB-2023-7200", "企业B", 2000)
    task_b = runner.create_task("批次B", str(root_b), rules)
    runner.run(task_b)
    second = db.list_arbitration_batches(task_b)[0]
    assert db.list_arbitration_conflicts(second["id"])
    runner.rollback_arbitration(second["batch_no"])
    # 只剩第一批结果，且字段值未被第二批影响。
    rows = db.query("SELECT * FROM results")
    assert len(rows) == 1 and rows[0]["arbitration_batch_id"] != second["id"]
    assert json.loads(rows[0]["data_json"])["applicant"] == "企业A"
    assert db.list_arbitration_conflicts(second["id"]) == []
    assert db.get_arbitration_batch(
        db.list_arbitration_batches(task_a)[0]["id"]
    )["status"] == "committed"
    # 第二批标记已撤销但档案保留可审计。
    assert db.get_arbitration_batch(second["id"])["status"] == "rolled_back"


def test_parallel_batch_writes_are_serialized(db, data_dir, tmp_path):
    """两个线程同时跑写同一逻辑表单的批次，提交被串行化且都不丢。"""
    runner = TaskRunner(db, data_dir / "files")
    rules = str(DEFAULT_RULE_DIR)
    roots = [
        _single_archive(tmp_path, f"p{i}", "SB-2023-7100", f"企业{i}",
                        1000 + i)
        for i in range(2)
    ]
    errors: list[Exception] = []

    def worker(root):
        try:
            tid = runner.create_task(f"并行-{root.name}", str(root), rules)
            runner.run(tid)
        except Exception as exc:  # pragma: no cover - 失败时暴露
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(root,)) for root in roots]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(db.list_arbitration_batches()) == 2
    # 任一后到批次记录了跨批冲突。
    all_conflicts = []
    for batch in db.list_arbitration_batches():
        all_conflicts.extend(db.list_arbitration_conflicts(batch["id"]))
    assert all_conflicts


# --------------------------------------------------------------------------
# 仲裁档案独立导出：不改动既有台账结构，被淘汰值可单独查看
# --------------------------------------------------------------------------
def test_arbitration_export_separate_files(
        db, data_dir, conflict_archive, archiver):
    runner = TaskRunner(db, data_dir / "files")
    task_id = runner.create_task(
        "导出仲裁", str(conflict_archive), str(DEFAULT_RULE_DIR)
    )
    runner.run(task_id)
    for fmt in ("csv", "json", "xlsx"):
        result = archiver.export_arbitration(task_id, fmt)
        path = Path(result["path"])
        assert path.exists() and result["field_records"] > 0
        if fmt == "json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert "field_records" in payload
            assert "cross_batch_conflicts" in payload
            losers = [r for r in payload["field_records"] if not r["adopted"]]
            assert losers
        if fmt == "xlsx":
            workbook = load_workbook(path)
            assert workbook.sheetnames == [
                "仲裁批次", "字段仲裁档案", "跨批冲突"
            ]
            workbook.close()
    # 既有台账导出结构保持 5 个 sheet 不变。
    ledger = archiver.export(task_id, "xlsx")
    workbook = load_workbook(ledger["path"])
    assert workbook.sheetnames == [
        "批次信息", "提取统计", "结果明细", "错误记录", "溯源索引"
    ]
    workbook.close()


def test_arbitration_export_missing_record_raises(db, archiver):
    task_id = db.create_task("空", "/tmp", "", {})
    with pytest.raises(ValueError, match="找不到仲裁记录"):
        archiver.export_arbitration(task_id, "json")
    with pytest.raises(ValueError, match="不支持"):
        archiver.export_arbitration(task_id, "pdf")


# --------------------------------------------------------------------------
# 撤销中途失败不产生部分成功（模拟日志镜像损坏）
# --------------------------------------------------------------------------
def test_rollback_failure_is_atomic(db, data_dir, conflict_archive, monkeypatch):
    runner = TaskRunner(db, data_dir / "files")
    task_id = runner.create_task(
        "原子撤销", str(conflict_archive), str(DEFAULT_RULE_DIR)
    )
    runner.run(task_id)
    batch = db.list_arbitration_batches(task_id)[0]
    result_count_before = db.query("SELECT COUNT(*) n FROM results")[0]["n"]

    real_restore = Arbiter._restore_entry

    def boom(self, entry, counter):
        if entry["entity"] == "traces":
            raise RuntimeError("模拟撤销中途失败")
        return real_restore(self, entry, counter)

    monkeypatch.setattr(Arbiter, "_restore_entry", boom)
    with pytest.raises(ArbitrationError, match="撤销中途失败"):
        runner.rollback_arbitration(batch["batch_no"])
    # 事务整体回滚，数据保持撤销前状态，没有半新半旧。
    assert db.query("SELECT COUNT(*) n FROM results")[0]["n"] == result_count_before
    assert db.get_arbitration_batch(batch["id"])["status"] == "committed"
    monkeypatch.undo()
    # 故障排除后可正常撤销。
    runner.rollback_arbitration(batch["batch_no"])
    assert db.get_arbitration_batch(batch["id"])["status"] == "rolled_back"


def test_restore_entry_handles_delete_and_corrupt_journal(db):
    """撤销日志的 delete 镜像可重插，缺失镜像时明确报错而非部分成功。"""
    arbiter = Arbiter(db)
    task_id = db.create_task("日志回退", "/tmp", "", {})
    result_id = db.store_result({
        "task_id": task_id, "form_type": "declare", "rule_id": "r",
        "rule_version": "v", "rule_content_hash": "h",
        "data": {"declare_no": "SB-1"}, "status": "valid",
        "source_rel_path": "x.html", "source_md5": "m", "version": 1,
    })
    row = db.get_result(result_id)
    counter = {"delete": 0, "insert": 0, "update": 0}
    # delete 操作携带整行镜像，撤销时把物理删除的行重插回来。
    db.execute("DELETE FROM results WHERE id = ?", (result_id,))
    arbiter._restore_entry(
        {"entity": "results", "entity_id": result_id,
         "operation": "delete", "before_json": json.dumps(row)},
        counter,
    )
    assert counter["insert"] == 1
    assert db.get_result(result_id)["data_json"] == row["data_json"]
    # update 缺镜像时报错。
    with pytest.raises(ArbitrationError, match="缺少更新前镜像"):
        arbiter._restore_entry(
            {"entity": "results", "entity_id": result_id,
             "operation": "update", "before_json": None},
            counter,
        )
    # 非法实体不能进日志（表名注入防护）。
    with pytest.raises(ValueError, match="不支持的实体"):
        db.journal(1, "evil_table; DROP TABLE results", 1, "insert", None)


def test_ruleset_version_recorded_in_batch(db, data_dir, conflict_archive):
    runner = TaskRunner(db, data_dir / "files")
    ruleset = load_rules(DEFAULT_RULE_DIR)
    task_id = runner.create_task(
        "规则版本", str(conflict_archive), str(ruleset.source_dir)
    )
    runner.run(task_id)
    batch = db.list_arbitration_batches(task_id)[0]
    assert batch["ruleset_signature"] == ruleset.signature
    versions = json.loads(batch["rule_versions_json"])
    assert {v["rule_id"] for v in versions} == {r["rule_id"] for r in ruleset.all()}
