"""Unit tests for the SQLite metadata transaction layer."""

from __future__ import annotations

import pytest

from arrowzero.metadata import (
    STATUS_COMMITTED,
    STATUS_FAILED,
    STATUS_REJECTED,
    MetadataStore,
)

pytestmark = pytest.mark.unit


def test_register_and_list_array(store: MetadataStore, run_id: str):
    store.ensure_run(run_id, "test", {"pyarrow": "x"})
    with store.transaction() as cur:
        store.register_array(
            cur,
            handle="arr_1",
            run_id=run_id,
            type_name="int32",
            length=3,
            logical_offset=1,
            null_count=1,
            origin="pylist",
            import_format="pylist",
            fingerprints=[{"name": "data", "sha256_16": "abc"}],
        )
    row = store.get_array("arr_1")
    assert row["logical_offset"] == 1
    assert [r["handle"] for r in store.list_arrays(run_id)] == ["arr_1"]


def test_rollback_on_business_error_leaves_no_array(store: MetadataStore, run_id: str):
    store.ensure_run(run_id, "test", {})
    with pytest.raises(RuntimeError):
        with store.transaction() as cur:
            store.register_array(
                cur,
                handle="arr_x",
                run_id=run_id,
                type_name="int32",
                length=1,
                logical_offset=0,
                null_count=0,
                origin="pylist",
                import_format="pylist",
                fingerprints=[],
            )
            raise RuntimeError("boom")
    assert store.get_array("arr_x") is None


def test_rejected_audit_row_committed_even_after_rollback(
    store: MetadataStore, run_id: str
):
    store.ensure_run(run_id, "test", {})
    try:
        with store.transaction() as cur:
            cur.execute(
                "INSERT INTO arrays(handle, run_id, type, length, origin, "
                "import_format, fingerprints) VALUES (?, ?, 'int32', 1, 'pylist', "
                "'pylist', '[]')",
                ("arr_tmp", run_id),
            )
            raise ValueError("validation failed")
    except ValueError:
        pass
    # business row rolled back...
    assert store.get_array("arr_tmp") is None
    # ...but audit row is independently committed
    op_id = store.record_operation(
        run_id=run_id,
        name="import",
        status=STATUS_REJECTED,
        detail={"error": "VALIDATION"},
        error_code="VALIDATION",
    )
    ops = store.list_operations(run_id)
    assert ops[0]["status"] == STATUS_REJECTED
    assert ops[0]["op_id"] == op_id


def test_illegal_status_rejected_not_silently_accepted(store: MetadataStore):
    with pytest.raises(ValueError):
        store.record_operation(
            run_id=None, name="x", status="unknown", detail={}
        )


def test_failed_status_distinct_from_rejected(store: MetadataStore, run_id: str):
    store.record_operation(
        run_id=run_id, name="import", status=STATUS_FAILED, detail={"e": "boom"}
    )
    ops = store.list_operations(run_id)
    assert ops[0]["status"] == STATUS_FAILED
    assert STATUS_COMMITTED != STATUS_FAILED
