"""格式适配层单元测试：校验、内容寻址确定性、Parquet 往返。"""
from __future__ import annotations

import pytest

from merge3.domain.models import FieldSpec, TableSpec
from merge3.adapters import arrow_format
from merge3.errors import ValidationError

SPEC = TableSpec("t", ["id"], [
    FieldSpec("id", "int64", False),
    FieldSpec("note", "string"),
    FieldSpec("score", "float64"),
])


def _rows():
    return [
        {"id": 2, "note": "b", "score": 2.5},
        {"id": 1, "note": "a", "score": 1.5},
    ]


def test_hash_is_order_independent_and_deterministic():
    _, h1, n1 = arrow_format.canonical_content(SPEC, _rows())
    _, h2, n2 = arrow_format.canonical_content(SPEC, list(reversed(_rows())))
    assert h1 == h2 and n1 == n2 == 2

    changed = [{"id": 1, "note": "a", "score": 1.5},
               {"id": 2, "note": "DIFF", "score": 2.5}]
    _, h3, _ = arrow_format.canonical_content(SPEC, changed)
    assert h3 != h1


def test_parquet_roundtrip(tmp_path):
    table, n = arrow_format.rows_to_table(_rows(), SPEC)
    p = tmp_path / "s.parquet"
    arrow_format.write_parquet(table, p)
    back = arrow_format.table_to_rows(arrow_format.read_parquet(p), SPEC)
    assert sorted(back, key=lambda r: r["id"]) == sorted(_rows(), key=lambda r: r["id"])


def test_duplicate_pk_rejected():
    rows = [{"id": 1, "note": "a", "score": 1.0},
            {"id": 1, "note": "b", "score": 2.0}]
    with pytest.raises(ValidationError, match="主键重复"):
        arrow_format.rows_to_table(rows, SPEC)


def test_null_pk_rejected():
    with pytest.raises(ValidationError, match="主键列"):
        arrow_format.rows_to_table([{"id": None, "note": "a", "score": 1.0}], SPEC)


def test_wrong_type_and_missing_column_rejected():
    with pytest.raises(ValidationError, match="期望 int64"):
        arrow_format.rows_to_table([{"id": "x", "note": "a", "score": 1.0}], SPEC)
    with pytest.raises(ValidationError, match="缺少列"):
        arrow_format.rows_to_table([{"id": 1, "note": "a"}], SPEC)
    with pytest.raises(ValidationError, match="未声明列"):
        arrow_format.rows_to_table([{"id": 1, "note": "a", "score": 1.0, "x": 1}], SPEC)


def test_empty_table_hash_and_roundtrip(tmp_path):
    table, n = arrow_format.rows_to_table([], SPEC)
    assert n == 0
    _, h1, _ = arrow_format.canonical_content(SPEC, [])
    _, h2, _ = arrow_format.canonical_content(SPEC, [])
    assert h1 == h2
    p = tmp_path / "e.parquet"
    arrow_format.write_parquet(table, p)
    assert arrow_format.read_parquet(p).num_rows == 0


def test_schema_requires_pk_in_fields():
    bad = TableSpec("t", ["missing"], [FieldSpec("id", "int64")])
    with pytest.raises(ValidationError, match="主键列未在字段中声明"):
        arrow_format.build_arrow_schema(bad)
