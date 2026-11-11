"""精确与模糊去重、逻辑删除及恢复测试。"""
import pytest
from archive_extractor.core.deduplicator import (
    Deduplicator,
    levenshtein,
    similarity,
)


def test_edit_distance_helpers():
    assert levenshtein("abc", "abc") == 0
    assert levenshtein("", "abc") == 3
    assert levenshtein("abc", "") == 3
    assert levenshtein("kitten", "sitting") == 3
    assert similarity("", "") == 1.0
    assert similarity("abc", "abd") == pytest.approx(2 / 3)


def test_exact_newer_wins_and_report(dedup_env):
    env = dedup_env
    old_id = env["seed"](env["task_id"], {"declare_no": "SB-1"}, mtime=100)
    new_id = env["seed"](env["task_id"], {"declare_no": "SB-1"}, mtime=200)
    report = env["dedup"].apply(
        env["task_id"], new_id, {"declare_no": "SB-1"}, 200, env["config"]
    )
    assert report["result_id"] == old_id
    assert report["duplicate_of"] == new_id
    old = env["db"].get_result(old_id)
    new = env["db"].get_result(new_id)
    assert old["is_deleted"] == 1 and old["status"] == "duplicate"
    assert new["is_deleted"] == 0 and new["status"] == "valid"
    assert env["dedup"].report(env["task_id"])[0]["reason"] == "primary_key"


def test_exact_older_loses(dedup_env):
    env = dedup_env
    old_id = env["seed"](env["task_id"], {"declare_no": "SB-2"}, mtime=300)
    new_id = env["seed"](env["task_id"], {"declare_no": "SB-2"}, mtime=200)
    report = env["dedup"].apply(
        env["task_id"], new_id, {"declare_no": "SB-2"}, 200, env["config"]
    )
    assert report["result_id"] == new_id
    assert report["duplicate_of"] == old_id
    assert env["db"].get_result(new_id)["is_deleted"] == 1
    assert env["db"].get_result(old_id)["is_deleted"] == 0


def test_fuzzy_match_uses_min_field_similarity(dedup_env):
    env = dedup_env
    env["seed"](env["task_id"], {
        "declare_no": "A", "applicant": "示例申报企业甲",
        "project_name": "非常长的示例项目名称甲",
    }, mtime=100)
    new_id = env["seed"](env["task_id"], {
        "declare_no": "B", "applicant": "示例申报企业乙",
        "project_name": "非常长的示例项目名称乙",
    }, mtime=200)
    config = dict(env["config"], threshold=0.8)
    report = env["dedup"].apply(env["task_id"], new_id, {
        "declare_no": "B", "applicant": "示例申报企业乙",
        "project_name": "非常长的示例项目名称乙",
    }, 200, config)
    assert report and report["reason"] == "fuzzy_content"
    assert 0.8 <= report["similarity"] < 1


def test_no_duplicate_below_threshold(dedup_env):
    env = dedup_env
    env["seed"](env["task_id"], {
        "declare_no": "A", "applicant": "甲公司", "project_name": "项目甲"
    }, mtime=100)
    new_id = env["seed"](env["task_id"], {
        "declare_no": "B", "applicant": "乙公司", "project_name": "项目乙"
    }, mtime=200)
    assert env["dedup"].apply(env["task_id"], new_id, {
        "declare_no": "B", "applicant": "乙公司", "project_name": "项目乙"
    }, 200, env["config"]) is None


def test_invalid_results_are_not_candidates(dedup_env):
    env = dedup_env
    env["seed"](env["task_id"], {"declare_no": "SB-9"}, mtime=100,
                status="invalid", level="error")
    new_id = env["seed"](env["task_id"], {"declare_no": "SB-9"}, mtime=50)
    assert env["dedup"].apply(
        env["task_id"], new_id, {"declare_no": "SB-9"}, 50, env["config"]
    ) is None


def test_restore_duplicate(dedup_env):
    env = dedup_env
    env["seed"](env["task_id"], {"declare_no": "SB-3"}, mtime=100)
    new_id = env["seed"](env["task_id"], {"declare_no": "SB-3"}, mtime=50)
    env["dedup"].apply(
        env["task_id"], new_id, {"declare_no": "SB-3"}, 50, env["config"]
    )
    assert env["dedup"].restore(new_id) is True
    row = env["db"].get_result(new_id)
    assert row["is_deleted"] == 0 and row["status"] == "valid"
    assert env["dedup"].restore(new_id) is False
    assert env["dedup"].restore(9999) is False


def test_nested_primary_key(db, make_task, seed_result):
    task_id = make_task("嵌套主键")
    seed_result(task_id, {"meta": {"no": "X1"}}, mtime=1)
    new_id = seed_result(task_id, {"meta": {"no": "X1"}}, mtime=2)
    report = Deduplicator(db).apply(
        task_id, new_id, {"meta": {"no": "X1"}}, 2,
        {"primary_key": ["meta.no"], "fuzzy_fields": []},
    )
    assert report and report["reason"] == "primary_key"
