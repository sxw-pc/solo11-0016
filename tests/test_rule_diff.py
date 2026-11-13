"""规则试跑与差异比对子命令的测试（隔离性、归因、兼容性、中断恢复）。"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from archive_extractor.cli import main
from archive_extractor.config.loader import DEFAULT_RULE_DIR, load_rules
from archive_extractor.core.rule_diff import (
    RuleDryRun,
    analyze_rules,
    load_rule_source,
    run_rule_diff,
    sweep_staging_dirs,
)
from archive_extractor.core.rule_diff.analyzer import (
    CAUSE_SELECTOR,
    CAUSE_TRANSFORM,
    CAUSE_VALIDATION,
)
from archive_extractor.core.rule_diff.comparator import (
    FIELD_REMOVED,
    LEVEL_CHANGED,
    VALUE_CHANGED,
)
from archive_extractor.utils.common import RuleError

MOCK = Path(__file__).resolve().parents[1] / "archive_extractor" / "mock_data"
DECLARE_RULE = DEFAULT_RULE_DIR / "declare_form.yaml"


def invoke(runner, data_dir, *args):
    return runner.invoke(main, ["--data-dir", str(data_dir), *args])


@pytest.fixture
def rule_variants(tmp_path):
    """基于内置申报规则造三份变体：选择器/转换/校验阈值各一处变化。"""
    import yaml

    base = yaml.safe_load(DECLARE_RULE.read_text(encoding="utf-8"))
    identical_dir = tmp_path / "rules_identical"
    selector_dir = tmp_path / "rules_selector"
    transform_dir = tmp_path / "rules_transform"
    validation_dir = tmp_path / "rules_validation"
    for directory in (identical_dir, selector_dir, transform_dir, validation_dir):
        directory.mkdir(parents=True, exist_ok=True)
        # 每个变体目录都包含完整规则集，只改动目标规则，
        # 避免无关表单因缺规则而产生“未匹配”噪声
        for rule_file in DEFAULT_RULE_DIR.glob("*.yaml"):
            shutil.copy(rule_file, directory / rule_file.name)

    selector_variant = json.loads(json.dumps(base, ensure_ascii=False))
    selector_variant["fields"]["project_name"]["selector"] = (
        '#declareForm .project-name-removed'
    )
    (selector_dir / "declare_form.yaml").write_text(
        yaml.safe_dump(selector_variant, allow_unicode=True), encoding="utf-8"
    )

    transform_variant = json.loads(json.dumps(base, ensure_ascii=False))
    transform_variant["fields"]["applicant"]["replacements"] = [
        {"pattern": "示例", "to": "样例"}
    ]
    (transform_dir / "declare_form.yaml").write_text(
        yaml.safe_dump(transform_variant, allow_unicode=True), encoding="utf-8"
    )

    validation_variant = json.loads(json.dumps(base, ensure_ascii=False))
    for validator in validation_variant["fields"]["amount"]["validators"]:
        if validator.get("type") == "range":
            validator["max"] = 1000
    (validation_dir / "declare_form.yaml").write_text(
        yaml.safe_dump(validation_variant, allow_unicode=True), encoding="utf-8"
    )
    return {
        "identical": identical_dir,
        "selector": selector_dir,
        "transform": transform_dir,
        "validation": validation_dir,
    }


def table_counts(db_path: Path) -> dict[str, int]:
    tables = (
        "tasks", "task_files", "results", "validations", "traces",
        "dedup_reports", "rules_versions", "exports",
    )
    con = sqlite3.connect(str(db_path))
    try:
        return {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
    finally:
        con.close()


# ---------- 规则加载与版本一致性 ----------

def test_single_rule_file_uses_same_version_algorithm():
    ruleset_dir = load_rules(DEFAULT_RULE_DIR)
    ruleset_file = load_rule_source(DECLARE_RULE)
    from_dir = ruleset_dir.get("declare_form")
    from_file = ruleset_file.get("declare_form")
    assert from_file["_effective_version"] == from_dir["_effective_version"]
    assert from_file["_content_hash"] == from_dir["_content_hash"]


def test_identical_rulesets_produce_identical_ledgers(tmp_path, rule_variants):
    # “同一版本”场景：两侧指向同一份规则文件，版本号、台账必须逐字段一致。
    # 注：现有 RuleSet 哈希输入包含 _source_path，复制到不同路径的规则
    # 哈希本就不同（与正式任务版本生成方式一致），故同版本用同一路径验证。
    report = run_rule_diff(
        MOCK, DEFAULT_RULE_DIR, DEFAULT_RULE_DIR,
        staging_base=tmp_path / "staging",
    )
    assert report["summary"]["changed_files"] == 0
    old_records = report["ledgers"]["old"]["records"]
    new_records = report["ledgers"]["new"]["records"]
    assert set(old_records) == set(new_records)
    for rel_path, old_record in old_records.items():
        new_record = new_records[rel_path]
        assert old_record["data"] == new_record["data"]
        assert old_record["status"] == new_record["status"]
        assert old_record["error_level"] == new_record["error_level"]
        assert old_record["rule_version"] == new_record["rule_version"]
        assert old_record["validations"] == new_record["validations"]


# ---------- 静态归因 ----------

def test_analyzer_classifies_selector_transform_validation(rule_variants):
    base = load_rules(DEFAULT_RULE_DIR)
    selector = analyze_rules(base, load_rules(rule_variants["selector"]))
    assert selector["affected_fields"]["project_name"] == [CAUSE_SELECTOR]

    transform = analyze_rules(base, load_rules(rule_variants["transform"]))
    assert transform["affected_fields"]["applicant"] == [CAUSE_TRANSFORM]

    validation = analyze_rules(base, load_rules(rule_variants["validation"]))
    assert validation["affected_fields"]["amount"] == [CAUSE_VALIDATION]


def test_analyzer_detects_added_and_removed_fields(tmp_path):
    import yaml

    base = yaml.safe_load(DECLARE_RULE.read_text(encoding="utf-8"))
    old_dir, new_dir = tmp_path / "old", tmp_path / "new"
    old_dir.mkdir(), new_dir.mkdir()
    shutil.copy(DECLARE_RULE, old_dir / "r.yaml")
    variant = json.loads(json.dumps(base, ensure_ascii=False))
    variant["fields"]["extra_note"] = {
        "label": "备注", "selector": ".note", "type": "text",
    }
    del variant["fields"]["scope"]
    (new_dir / "r.yaml").write_text(
        yaml.safe_dump(variant, allow_unicode=True), encoding="utf-8"
    )
    analysis = analyze_rules(load_rules(old_dir), load_rules(new_dir))
    changes = {
        item["field"]: item["change"]
        for item in analysis["rules"][0]["field_changes"]
    }
    assert changes["extra_note"] == "field_added"
    assert changes["scope"] == "field_removed"


# ---------- 端到端差异定位 ----------

def test_selector_change_pinpoints_field_and_files(tmp_path, rule_variants):
    report = run_rule_diff(
        MOCK, DEFAULT_RULE_DIR, rule_variants["selector"],
        staging_base=tmp_path / "staging",
    )
    details = report["changed_file_details"]
    # 仅申报类 normal 文件含 project_name 字段，且应被精确定位
    affected_fields = {
        diff["field"]
        for entry in details for diff in entry["field_diffs"]
    }
    assert "project_name" in affected_fields
    for entry in details:
        for diff in entry["field_diffs"]:
            if diff["field"] == "project_name":
                assert diff["change"] in {VALUE_CHANGED, FIELD_REMOVED}
                assert CAUSE_SELECTOR in diff["causes"]
        assert entry["source_md5_old"] == entry["source_md5_new"]
        assert len(entry["source_md5_new"]) == 32
    # 受影响文件清单与明细一致，且全部带 MD5
    assert report["affected_files"]
    for item in report["affected_files"]:
        assert item["md5"] and len(item["md5"]) == 32


def test_transform_change_reports_value_changed(tmp_path, rule_variants):
    report = run_rule_diff(
        MOCK, DEFAULT_RULE_DIR, rule_variants["transform"],
        staging_base=tmp_path / "staging",
    )
    hits = [
        diff for entry in report["changed_file_details"]
        for diff in entry["field_diffs"]
        if diff["field"] == "applicant"
    ]
    assert hits
    assert all(diff["change"] == VALUE_CHANGED for diff in hits)
    assert all(CAUSE_TRANSFORM in diff["causes"] for diff in hits)
    assert all("样例" in str(diff["new_value"]) for diff in hits)


def test_validation_threshold_change_reports_level_changed(
    tmp_path, rule_variants
):
    report = run_rule_diff(
        MOCK, DEFAULT_RULE_DIR, rule_variants["validation"],
        staging_base=tmp_path / "staging",
    )
    level_hits = [
        diff for entry in report["changed_file_details"]
        for diff in entry["field_diffs"]
        if diff["change"] == LEVEL_CHANGED and diff["field"] == "amount"
    ]
    assert level_hits
    assert all(CAUSE_VALIDATION in diff["causes"] for diff in level_hits)
    # 阈值变化不应产生任何值变化
    value_hits = [
        diff for entry in report["changed_file_details"]
        for diff in entry["field_diffs"]
        if diff["change"] == VALUE_CHANGED and diff["field"] == "amount"
    ]
    assert not value_hits


# ---------- 隔离性 ----------

def test_dry_run_never_writes_main_database(tmp_path, rule_variants):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    runner = CliRunner(mix_stderr=False)
    # 先在主库跑一个正式任务，制造既有数据
    result = invoke(runner, data_dir, "run", str(MOCK), "--limit", "5")
    assert result.exit_code == 0, result.output
    db_path = data_dir / "archive.db"
    before = table_counts(db_path)
    snapshot_root = data_dir / "files"
    snapshot_before = sorted(
        str(p.relative_to(snapshot_root))
        for p in snapshot_root.rglob("*") if p.is_file()
    )

    output = tmp_path / "report.json"
    result = invoke(
        runner, data_dir, "rule-diff", str(MOCK),
        str(DEFAULT_RULE_DIR), str(rule_variants["selector"]),
        "-o", str(output),
    )
    assert result.exit_code == 0, result.output
    assert table_counts(db_path) == before
    snapshot_after = sorted(
        str(p.relative_to(snapshot_root))
        for p in snapshot_root.rglob("*") if p.is_file()
    )
    assert snapshot_after == snapshot_before
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["isolation_check"]["main_db_unchanged"] is True
    assert payload["isolation_check"]["snapshot_dir_unchanged"] is True


def test_temp_staging_cleaned_after_success_and_failure(tmp_path):
    staging_base = tmp_path / "staging"
    staging_base.mkdir()
    run_rule_diff(
        MOCK, DEFAULT_RULE_DIR, DEFAULT_RULE_DIR,
        staging_base=staging_base,
    )
    assert not list(staging_base.glob("ae_dryrun_*"))
    with pytest.raises(RuleError):
        run_rule_diff(
            MOCK, DEFAULT_RULE_DIR, tmp_path / "not-exist-rules",
            staging_base=staging_base,
        )
    assert not list(staging_base.glob("ae_dryrun_*"))


def test_sweep_removes_crashed_staging_dirs(tmp_path):
    staging_base = tmp_path / "staging"
    staging_base.mkdir()
    leftover = staging_base / "ae_dryrun_99999_fakeleftover"
    leftover.mkdir()
    (leftover / "dryrun.db").write_text("x", encoding="utf-8")
    cleaned = sweep_staging_dirs(staging_base)
    assert str(leftover) in cleaned
    assert not leftover.exists()


def test_interrupted_process_leaves_no_dirty_data(tmp_path, monkeypatch):
    """模拟试跑中途收到终止信号：临时库被清理，主库不受影响。"""
    staging_base = tmp_path / "staging"
    staging_base.mkdir()
    dry = RuleDryRun(
        load_rules(DEFAULT_RULE_DIR), MOCK, staging_base=staging_base
    )
    created: list[Path] = []

    def explode():
        created.append(dry.staging_path)
        assert dry.staging_path.exists()
        # 模拟进程收到终止信号时执行的清理路径
        dry.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(
        dry, "_install_signal_handlers",
        lambda: (created.append(dry.staging_path), explode())[1],
    )
    with pytest.raises(KeyboardInterrupt):
        dry.execute()
    for path in created:
        assert not path.exists()
    assert not list(staging_base.glob("ae_dryrun_*"))


def test_staging_dir_uses_unique_isolated_db(tmp_path):
    staging_base = tmp_path / "staging"
    staging_base.mkdir()
    with RuleDryRun(
        load_rules(DEFAULT_RULE_DIR), MOCK, staging_base=staging_base
    ) as dry:
        ledger = dry.execute()
        assert ledger["records"]
        db_path = dry.staging_path / "dryrun.db"
        assert db_path.exists()
        main_con = sqlite3.connect(str(db_path))
        try:
            # 临时库拥有完整业务表，但与主库物理隔离
            names = {
                row[0] for row in main_con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert {"results", "validations", "traces", "dedup_reports"} <= names
        finally:
            main_con.close()
    assert not dry.staging_path.exists()


def test_real_subprocess_sigterm_then_restart_sweeps_clean(tmp_path):
    """真实子进程在试跑中被 SIGTERM 杀死：信号处理器清理临时目录；
    若被强杀留下残留，下一次启动时 sweep 自动清扫，主流程恢复正常。"""
    import subprocess
    import sys
    import time

    staging_base = tmp_path / "staging"
    staging_base.mkdir()
    worker = tmp_path / "sigterm_worker.py"
    project_root = Path(__file__).resolve().parents[1]
    worker.write_text(
        "\n".join([
            "import sys, time",
            f"sys.path.insert(0, {str(project_root)!r})",
            "from pathlib import Path",
            "from archive_extractor.config.loader import load_rules",
            "from archive_extractor.core.rule_diff import RuleDryRun",
            f"MOCK = {str(MOCK)!r}",
            f"RULES = {str(DEFAULT_RULE_DIR)!r}",
            f"STAGING = {str(staging_base)!r}",
            "dry = RuleDryRun(load_rules(RULES), MOCK, staging_base=STAGING)",
            "print(dry.staging_path, flush=True)",
            "try:",
            "    dry.execute()",
            "finally:",
            "    dry.close()",
        ]),
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(worker)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(project_root),
    )
    line = proc.stdout.readline().strip()
    assert line.startswith(str(staging_base))
    created = Path(line)
    deadline = time.time() + 10
    while not (created / "dryrun.db").exists() and time.time() < deadline:
        time.sleep(0.05)
    assert (created / "dryrun.db").exists()
    proc.terminate()
    proc.wait(timeout=15)
    assert proc.returncode != 0
    # SIGTERM 被自定义处理器捕获，临时目录已被清空
    assert not created.exists()

    # 模拟强杀（SIGKILL 无法捕获）：手工制造一个残留目录，
    # 再启动一次正常试跑，启动清扫应把残留带走且新试跑成功
    leftover = staging_base / f"ae_dryrun_{proc.pid}_killed"
    leftover.mkdir()
    (leftover / "dryrun.db").write_text("x", encoding="utf-8")
    report = run_rule_diff(
        MOCK, DEFAULT_RULE_DIR, DEFAULT_RULE_DIR, staging_base=staging_base
    )
    assert not leftover.exists()
    assert report["summary"]["total_files"] == 80
    assert report["staging"]["swept_leftovers"] == [str(leftover)]
    assert not list(staging_base.glob("ae_dryrun_*"))


# ---------- CLI 与报告结构 ----------

def test_cli_rule_diff_end_to_end(tmp_path, rule_variants):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    output = tmp_path / "diff" / "report.json"
    result = invoke(
        CliRunner(mix_stderr=False), data_dir, "rule-diff", str(MOCK),
        str(rule_variants["identical"]), str(DEFAULT_RULE_DIR),
        "-o", str(output), "--concurrency", "2",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["summary"]["total_files"] == 80
    assert payload["summary"]["changed_files"] == 0
    assert {item["rel_path"] for item in payload["input"]["scanned_files"]}
    for item in payload["input"]["scanned_files"]:
        assert len(item["md5"]) == 32
    assert "rule_analysis" in payload and "ledgers" in payload


def test_cli_rejects_missing_rule_path(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    result = invoke(
        CliRunner(mix_stderr=False), data_dir, "rule-diff", str(MOCK),
        str(tmp_path / "nope"), str(DEFAULT_RULE_DIR),
        "-o", str(tmp_path / "r.json"),
    )
    assert result.exit_code != 0


def test_report_contains_rule_versions_from_both_sides(tmp_path, rule_variants):
    report = run_rule_diff(
        MOCK, rule_variants["selector"], rule_variants["selector"],
        staging_base=tmp_path / "staging",
    )
    analysis = report["rule_analysis"]
    assert analysis["old_versions"][0]["version"].count("+") == 1
    assert analysis["new_versions"][0]["version"] == analysis["old_versions"][0][
        "version"
    ]


# ---------- 分析器与比对器的细粒度分支 ----------

def _write_rule_set(tmp_path: Path, name: str, rule: dict) -> Path:
    import yaml

    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "r.yaml").write_text(
        yaml.safe_dump(rule, allow_unicode=True), encoding="utf-8"
    )
    return directory


def test_analyzer_rule_added_removed_match_dedup_and_object(tmp_path):
    import yaml

    base = yaml.safe_load(DECLARE_RULE.read_text(encoding="utf-8"))
    old_dir = _write_rule_set(tmp_path, "old_a", base)

    added = json.loads(json.dumps(base, ensure_ascii=False))
    added["rule_id"] = "extra_form"
    added["match"] = {"marker": "EXTRA-MARKER"}
    new_dir = tmp_path / "new_a"
    new_dir.mkdir()
    shutil.copy(DECLARE_RULE, new_dir / "declare_form.yaml")
    (new_dir / "extra.yaml").write_text(
        yaml.safe_dump(added, allow_unicode=True), encoding="utf-8"
    )
    analysis = analyze_rules(load_rules(old_dir), load_rules(new_dir))
    by_id = {entry["rule_id"]: entry for entry in analysis["rules"]}
    assert by_id["extra_form"]["status"] == "rule_added"

    # 规则删除
    removed_dir = tmp_path / "only_one"
    removed_dir.mkdir()
    shutil.copy(
        new_dir / "extra.yaml", removed_dir / "extra.yaml"
    )
    analysis2 = analyze_rules(load_rules(new_dir), load_rules(removed_dir))
    by_id2 = {entry["rule_id"]: entry for entry in analysis2["rules"]}
    assert by_id2["declare_form"]["status"] == "rule_removed"

    # match 与 dedup 变化
    match_variant = json.loads(json.dumps(base, ensure_ascii=False))
    match_variant["match"] = {"marker": "OTHER"}
    match_variant["dedup"]["threshold"] = 0.8
    match_dir = _write_rule_set(tmp_path, "match_v", match_variant)
    analysis3 = analyze_rules(load_rules(old_dir), load_rules(match_dir))
    entry = analysis3["rules"][0]
    assert entry["match_changed"] is True
    assert entry["dedup_changed"] is True
    assert "declare_form" in analysis3["match_changed_rules"]


def test_analyzer_object_child_field_changes(tmp_path):
    import yaml

    base = yaml.safe_load(DECLARE_RULE.read_text(encoding="utf-8"))
    variant = json.loads(json.dumps(base, ensure_ascii=False))
    variant["fields"]["contact"]["fields"]["email"]["attr"] = "data-mail"
    variant["fields"]["contact"]["fields"]["new_child"] = {
        "label": "分机", "selector": ".ext", "type": "text",
    }
    old_dir = _write_rule_set(tmp_path, "obj_old", base)
    new_dir = _write_rule_set(tmp_path, "obj_new", variant)
    analysis = analyze_rules(load_rules(old_dir), load_rules(new_dir))
    fields = {
        item["field"]: item for item in analysis["rules"][0]["field_changes"]
    }
    assert fields["contact.email"]["causes"] == [CAUSE_SELECTOR]
    assert fields["contact.new_child"]["change"] == "field_added"


def test_analyzer_cross_field_validation_change_attributed(tmp_path):
    import yaml

    base = yaml.safe_load(DECLARE_RULE.read_text(encoding="utf-8"))
    variant = json.loads(json.dumps(base, ensure_ascii=False))
    variant["validations"][0]["level"] = "warning"
    old_dir = _write_rule_set(tmp_path, "xval_old", base)
    new_dir = _write_rule_set(tmp_path, "xval_new", variant)
    analysis = analyze_rules(load_rules(old_dir), load_rules(new_dir))
    assert analysis["rules"][0]["validation_changes"]


def test_comparator_handles_failed_files_and_rule_switch():
    from archive_extractor.core.rule_diff.comparator import compare_ledgers

    def ledger(records):
        return {"records": records}

    old = ledger({
        "a.html": {
            "source_rel_path": "a.html", "source_md5": "m1",
            "form_type": "declare", "rule_id": "declare_form",
            "rule_version": "1+old", "rule_content_hash": "h1",
            "status": "failed", "error_level": "error", "is_deleted": False,
            "version": 0, "data": {}, "validations": [],
            "extract_error": "未匹配到任何已配置的表单规则",
        },
        "b.html": {
            "source_rel_path": "b.html", "source_md5": "m2",
            "form_type": "declare", "rule_id": "declare_form",
            "rule_version": "1+old", "rule_content_hash": "h1",
            "status": "valid", "error_level": "", "is_deleted": False,
            "version": 1, "data": {"f": 1}, "validations": [],
            "extract_error": None,
        },
    })
    new = ledger({
        "a.html": {
            "source_rel_path": "a.html", "source_md5": "m1",
            "form_type": "declare", "rule_id": "declare_form",
            "rule_version": "1+new", "rule_content_hash": "h2",
            "status": "valid", "error_level": "", "is_deleted": False,
            "version": 1, "data": {"f": 9}, "validations": [],
            "extract_error": None,
        },
        "b.html": {
            "source_rel_path": "b.html", "source_md5": "m2",
            "form_type": "other", "rule_id": "other_form",
            "rule_version": "1+new", "rule_content_hash": "h2",
            "status": "valid", "error_level": "", "is_deleted": False,
            "version": 1, "data": {"f": 1}, "validations": [],
            "extract_error": None,
        },
    })
    analysis = {
        "rules": [], "affected_fields": {}, "match_changed_rules": [],
    }
    report = compare_ledgers(old, new, analysis)
    details = {d["source_rel_path"]: d for d in report["changed_file_details"]}
    assert "extraction_status_changed" in details["a.html"]["change_types"]
    assert "rule_match_changed" in details["b.html"]["change_types"]
    assert report["summary"]["changed_files"] == 2


def test_runner_rejects_when_input_manifests_differ(tmp_path, monkeypatch):
    from archive_extractor.core.rule_diff import runner as runner_mod

    calls = {"n": 0}

    def fake_scan(root):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"a.html": {"rel_path": "a.html", "md5": "x", "size": 1}}
        return {"a.html": {"rel_path": "a.html", "md5": "y", "size": 1}}

    monkeypatch.setattr(runner_mod, "_scan_manifest", fake_scan)
    with pytest.raises(RuleError, match="输入文件清单不一致"):
        runner_mod.run_rule_diff(
            MOCK, DEFAULT_RULE_DIR, DEFAULT_RULE_DIR,
            staging_base=tmp_path / "staging", clean_staging=False,
        )


def test_write_report_creates_parent_dirs(tmp_path):
    from archive_extractor.core.rule_diff import write_report

    target = tmp_path / "nested" / "deep" / "report.json"
    path = write_report({"ok": True}, target)
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}


def test_dry_run_nested_signal_registration_and_idempotent_close(tmp_path):
    from archive_extractor.core.rule_diff import engine as engine_mod

    staging_base = tmp_path / "staging"
    staging_base.mkdir()
    outer = RuleDryRun(
        load_rules(DEFAULT_RULE_DIR), MOCK, staging_base=staging_base
    )
    inner = RuleDryRun(
        load_rules(DEFAULT_RULE_DIR), MOCK, staging_base=staging_base
    )
    outer._install_signal_handlers()
    assert engine_mod._SIGNAL_DEPTH == 1
    inner._install_signal_handlers()
    assert engine_mod._SIGNAL_DEPTH == 2
    # 内层结束时不能恢复处理器，外层仍受保护
    inner._restore_signal_handlers()
    assert engine_mod._SIGNAL_DEPTH == 1
    outer._restore_signal_handlers()
    assert engine_mod._SIGNAL_DEPTH == 0
    # 未安装过信号时恢复应为空操作
    spare = RuleDryRun(
        load_rules(DEFAULT_RULE_DIR), MOCK, staging_base=staging_base
    )
    spare._restore_signal_handlers()
    # close 幂等，且允许在未 execute 时直接关闭
    spare.close()
    spare.close()
    assert not spare.staging_path.exists()


def test_dry_run_close_swallows_db_close_error(tmp_path, monkeypatch):
    staging_base = tmp_path / "staging"
    staging_base.mkdir()
    dry = RuleDryRun(
        load_rules(DEFAULT_RULE_DIR), MOCK, staging_base=staging_base
    )
    dry.execute()

    class BrokenDB:
        def close(self):
            raise RuntimeError("boom")

    dry._db = BrokenDB()
    dry.close()  # 不应抛出
    assert not dry.staging_path.exists()


def test_comparator_overall_level_change_without_field_diffs():
    from archive_extractor.core.rule_diff.comparator import compare_ledgers

    def record(level, issue_level):
        return {
            "source_rel_path": "c.html", "source_md5": "m",
            "form_type": "declare", "rule_id": "declare_form",
            "rule_version": "1", "rule_content_hash": "h",
            "status": "valid" if not issue_level else "invalid",
            "error_level": level, "is_deleted": False, "version": 1,
            "data": {"f": 1},
            "validations": (
                [{"level": issue_level, "field": "g", "message": "x"}]
                if issue_level else []
            ),
            "extract_error": None,
        }

    old = {"records": {"c.html": record("", "")}}
    new = {"records": {"c.html": record("error", "error")}}
    report = compare_ledgers(
        old, new, {"rules": [{"validation_changes": True}],
                   "affected_fields": {}, "match_changed_rules": []}
    )
    entry = report["changed_file_details"][0]
    assert entry["change_types"] == ["level_changed"]
    assert entry["field_diffs"][0]["field"] is None
    assert entry["field_diffs"][0]["causes"] == ["validation"]
