"""格式适配层测试：模式严格性、原子文件、指纹校验、失败类别。"""

from __future__ import annotations

import pytest

from lake_txn import errors, format_adapter
from lake_txn.format_adapter import ColumnSpec, write_parquet_atomic

SPECS = [
    ColumnSpec("order_id", "int64"),
    ColumnSpec("region", "string"),
    ColumnSpec("amount", "float64"),
]


def test_round_trip(tmp_path):
    wf = write_parquet_atomic(
        [{"order_id": 1, "region": "cn", "amount": 2.5}], SPECS, tmp_path, "a.parquet"
    )
    assert wf.row_count == 1
    sha, rows = format_adapter.verify_parquet(wf.path, SPECS, wf.sha256, 1)
    assert rows == 1
    assert format_adapter.read_rows(wf.path) == [{"order_id": 1, "region": "cn", "amount": 2.5}]


def test_unsupported_type_rejected(tmp_path):
    with pytest.raises(errors.DomainError) as ei:
        format_adapter.build_schema([ColumnSpec("x", "decimal(10,2)")])
    assert ei.value.reason_code == errors.UNSUPPORTED_TYPE


def test_extra_field_rejected(tmp_path):
    with pytest.raises(errors.DomainError) as ei:
        format_adapter.records_to_table(
            [{"order_id": 1, "region": "cn", "amount": 1.0, "unexpected": 9}], SPECS
        )
    assert ei.value.reason_code == errors.VALIDATION_ERROR


def test_bool_not_silently_coerced_into_int(tmp_path):
    with pytest.raises(errors.DomainError) as ei:
        format_adapter.records_to_table(
            [{"order_id": True, "region": "cn", "amount": 1.0}], SPECS
        )
    assert ei.value.reason_code == errors.VALIDATION_ERROR


def test_string_column_rejects_int(tmp_path):
    with pytest.raises(errors.DomainError) as ei:
        format_adapter.records_to_table(
            [{"order_id": 1, "region": 7, "amount": 1.0}], SPECS
        )
    assert ei.value.reason_code == errors.VALIDATION_ERROR


def test_hash_mismatch_detected(tmp_path):
    wf = write_parquet_atomic(
        [{"order_id": 1, "region": "cn", "amount": 1.0}], SPECS, tmp_path, "a.parquet"
    )
    with pytest.raises(errors.DomainError) as ei:
        format_adapter.verify_parquet(wf.path, SPECS, "deadbeef", 1)
    assert ei.value.reason_code == errors.FILE_HASH_MISMATCH


def test_schema_mismatch_detected(tmp_path):
    wf = write_parquet_atomic(
        [{"order_id": 1, "region": "cn", "amount": 1.0}], SPECS, tmp_path, "a.parquet"
    )
    other = [ColumnSpec("order_id", "int64"), ColumnSpec("region", "string")]
    with pytest.raises(errors.DomainError) as ei:
        format_adapter.verify_parquet(wf.path, other, wf.sha256, 1)
    assert ei.value.reason_code == errors.SCHEMA_MISMATCH


def test_row_count_mismatch_detected(tmp_path):
    wf = write_parquet_atomic(
        [{"order_id": 1, "region": "cn", "amount": 1.0},
         {"order_id": 2, "region": "cn", "amount": 3.0}],
        SPECS, tmp_path, "a.parquet",
    )
    with pytest.raises(errors.DomainError) as ei:
        format_adapter.verify_parquet(wf.path, SPECS, wf.sha256, 1)
    assert ei.value.reason_code == errors.STAGE_VALIDATION_FAILED


def test_atomic_write_leaves_no_tmp_file_on_success(tmp_path):
    write_parquet_atomic(
        [{"order_id": 1, "region": "cn", "amount": 1.0}], SPECS, tmp_path, "a.parquet"
    )
    assert {p.name for p in tmp_path.iterdir()} == {"a.parquet"}
