"""格式适配层：类型校验、主键约束与 Parquet 往返/幂等。"""
from __future__ import annotations

import pytest

from table_merge.errors import SnapshotFormatError
from table_merge.format_adapter import (
    read_parquet,
    validate_rows,
    write_parquet,
)
from table_merge.models import TableSchema

from .conftest import EMP_SCHEMA, row

SCHEMA = TableSchema.from_dict(EMP_SCHEMA)


def test_parquet_roundtrip_preserves_types_and_order(tmp_path):
    rows = [row(2, "b", "sh", 20.5), row(1, "a", "bj", None)]
    digest1 = write_parquet(rows, SCHEMA, tmp_path / "a.parquet")
    back = read_parquet(tmp_path / "a.parquet")
    # 写出前按主键排序 -> id=1 在前
    assert [r["id"] for r in back] == [1, 2]
    assert back[1]["score"] == 20.5 and isinstance(back[1]["score"], float)
    assert back[0]["score"] is None
    assert back[0]["active"] is True
    # 相同内容二次写出 -> 字节一致（确定性）
    digest2 = write_parquet(rows, SCHEMA, tmp_path / "b.parquet")
    assert digest1 == digest2
    assert (tmp_path / "a.parquet").read_bytes() == (tmp_path / "b.parquet").read_bytes()


@pytest.mark.parametrize("bad_row,reason", [
    ({"id": 1.5, "name": "a", "city": "x", "score": 1.0, "active": True}, "float-into-int"),
    ({"id": True, "name": "a", "city": "x", "score": 1.0, "active": True}, "bool-into-int"),
    ({"id": 1, "name": 2, "city": "x", "score": 1.0, "active": True}, "int-into-string"),
    ({"id": 1, "name": "a", "city": "x", "score": 1.0, "active": "yes"}, "string-into-bool"),
    ({"id": 1, "name": "a", "city": "x", "score": float("nan"), "active": True}, "nan"),
    ({"id": 1, "name": "a", "city": "x", "score": float("inf"), "active": True}, "inf"),
    ({"id": None, "name": "a", "city": "x", "score": 1.0, "active": True}, "null-pk"),
])
def test_invalid_scalar_types_are_classified_failures(bad_row, reason):
    with pytest.raises(SnapshotFormatError) as ei:
        validate_rows([bad_row], SCHEMA)
    assert ei.value.error_code == "SNAPSHOT_FORMAT"
    assert reason  # reason 仅用于用例命名可读性


def test_unknown_missing_columns_and_duplicate_pk_are_rejected():
    with pytest.raises(SnapshotFormatError) as ei:
        validate_rows([{**row(1, "a", "x", 1), "extra": 1}], SCHEMA)
    assert "extra" in ei.value.details["extra"]

    with pytest.raises(SnapshotFormatError) as ei:
        validate_rows([{"id": 1, "name": "a", "city": "x",
                        "active": True}], SCHEMA)
    assert ei.value.details["missing"] == ["score"]  # 缺列给出明确清单

    with pytest.raises(SnapshotFormatError) as ei:
        validate_rows([row(1, "a", "x", 1), row(1, "b", "y", 2)], SCHEMA)
    assert ei.value.details["key"] == [1]


def test_schema_construction_validates_itself():
    with pytest.raises(ValueError):
        TableSchema.from_dict({"table": "t",
                               "columns": [{"name": "id", "type": "json"}],
                               "primary_key": ["id"]})
    with pytest.raises(ValueError):
        TableSchema.from_dict({"table": "t",
                               "columns": [{"name": "id", "type": "int64"}],
                               "primary_key": []})
    with pytest.raises(ValueError):
        TableSchema.from_dict({"table": "t",
                               "columns": [{"name": "id", "type": "int64"}],
                               "primary_key": ["missing"]})
