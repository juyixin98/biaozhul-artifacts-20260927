"""SQLite 元数据事务测试。"""
import pytest

from colaudit.catalog import Catalog


def _report(run_id="r1", dataset="d", bad=False):
    verdict = {
        "file": "data.parquet",
        "row_group": 0,
        "scope": "page",
        "page": 0,
        "column_name": "score",
        "verdict": "minmax_mismatch" if bad else "ok",
        "trusted": not bad,
        "failure": "minmax_mismatch" if bad else None,
        "detail": {"diffs": ["min"] if bad else []},
    }
    return {
        "run_id": run_id,
        "dataset": dataset,
        "summary": {"trusted": 0 if bad else 1},
        "verdicts": [verdict],
        "diagnostics": [
            {
                "request_id": "req1",
                "dataset": dataset,
                "file": "data.parquet",
                "row_group": 0,
                "scope": "page",
                "page": 0,
                "column_name": "score",
                "code": "minmax_mismatch" if bad else "ok",
                "decision": "reject" if bad else "accept",
                "message": "x",
                "state": {"k": "v"},
            }
        ],
    }


def test_register_and_list(catalog, tmp_path):
    catalog.register_dataset(
        "d1", tmp_path / "d1",
        columns=[{"name": "score", "logical_type": "float"}],
        sensitive=["score"],
    )
    row = catalog.get_dataset("d1")
    assert row["sensitive"] == ["score"]
    assert [d["name"] for d in catalog.list_datasets()] == ["d1"]


def test_report_roundtrip_and_latest_run(catalog, tmp_path):
    catalog.register_dataset("d", tmp_path, columns=[], sensitive=[])
    catalog.save_report(_report(run_id="r1", bad=True))
    catalog.save_report(_report(run_id="r2", bad=False))
    loaded = catalog.load_report("r1")
    assert loaded["verdicts"][0]["trusted"] is False
    assert loaded["diagnostics"][0]["state"] == {"k": "v"}
    assert catalog.latest_run_id("d") == "r2"


def test_transaction_rollback_on_failure(tmp_path):
    db = Catalog(tmp_path / "x.db")
    # 故意触发外键错误: 引用不存在的数据集
    with pytest.raises(Exception):
        db.save_report(_report(run_id="rx", dataset="ghost"))
    assert db.load_report("rx") is None  # 整体回滚, 无残留
    # 坏报告之后好报告仍可写入
    db.register_dataset("d", tmp_path, columns=[], sensitive=[])
    db.save_report(_report(run_id="ry", dataset="d"))
    assert db.latest_run_id("d") == "ry"


def test_trusted_page_verdicts_index(catalog, tmp_path):
    catalog.register_dataset("d", tmp_path, columns=[], sensitive=[])
    catalog.save_report(_report(run_id="r3"))
    idx = catalog.trusted_page_verdicts("r3")
    row = idx[("data.parquet", 0, 0)]["score"]
    assert row["trusted"] is True
