"""Unit tests: SQLite metadata transactions."""
from __future__ import annotations

import json

import pytest

from app.errors import ErrorCategory, LayoutError
from app.store.metadata import MetadataStore

pytestmark = pytest.mark.unit


def test_record_operation_rolls_back_when_work_raises(tmp_path):
    store = MetadataStore(tmp_path / "m.db")
    marker = {"called": False}

    def work():
        marker["called"] = True
        store.upsert_column(
            column_id="col_x", type_name="int32", logical_offset=0,
            logical_length=1, null_count=0, source="test", parent_ids=[],
            buffers=[], run_id="r1")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        store.record_operation(
            op_id="op1", run_id="r1", op_type="import", status="ok",
            input_fp="fp", result_id="col_x", detail={}, error=None, work=work)
    assert marker["called"] is True
    # Column insert rolled back together with the operation journal.
    assert store.get_column("col_x") is None
    assert store.get_operation("op1") is None
    store.close()


def test_record_audit_persists_failed_validation_independently(tmp_path):
    store = MetadataStore(tmp_path / "m2.db")
    err = LayoutError(ErrorCategory.OFFSETS_NOT_MONOTONIC, "decreasing", detail={"index": 2})
    store.record_audit(
        op_id="op_bad", run_id="r9", op_type="validate", status="error",
        input_fp="fpdeadbeef", detail={"type": "utf8"}, error=err.to_dict())
    row = store.get_operation("op_bad")
    assert row is not None
    assert row["status"] == "error"
    assert json.loads(row["error_json"])["category"] == "offsets_not_monotonic"
    assert row["input_fp"] == "fpdeadbeef"
    store.close()


def test_attach_validation_roundtrip(tmp_path):
    store = MetadataStore(tmp_path / "m3.db")
    store.record_audit(op_id="opv", run_id="r", op_type="validate", status="invalid",
                       input_fp=None, detail={}, error=None)
    report = {"ok": False, "failure_categories": ["data_too_short"], "checks": []}
    store.attach_validation("opv", report)
    cur = store.conn.execute("SELECT ok, report_json FROM validations WHERE op_id=?", ("opv",))
    ok, raw = cur.fetchone()
    assert ok == 0
    assert json.loads(raw)["failure_categories"] == ["data_too_short"]
    store.close()
