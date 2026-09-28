"""适配层测试：三种格式、缺列补 NULL、非标量拒绝、解析错误定位。"""
from __future__ import annotations

import pytest

from merge_engine.adapter import load_source
from merge_engine.errors import SourceFormatError


def test_records_sparse_rows_get_null_fill():
    # 第 1 行缺 amount，第 2 行缺 status：按本批列并集补 NULL（键列不补）
    batch = load_source(
        {"format": "records", "records": [
            {"k1": "a", "status": "X"},
            {"k1": "b", "amount": 9},
        ]},
        key_columns=("k1",),
    )
    assert batch.columns == ("k1", "amount", "status")
    assert batch.rows[0].values == {"k1": "a", "status": "X", "amount": None}
    assert batch.rows[1].values == {"k1": "b", "amount": 9, "status": None}
    assert batch.rows[0].rownum == 1 and batch.rows[1].rownum == 2


def test_ndjson_parse_error_has_line_location():
    with pytest.raises(SourceFormatError) as ei:
        load_source({"format": "ndjson", "content": '{"k1":1}\n{not json}\n'},
                    key_columns=("k1",))
    assert ei.value.code == "SOURCE_FORMAT_ERROR"
    assert ei.value.details["line"] == 2


def test_parquet_roundtrip(tmp_path, make_parquet):
    path = make_parquet("src.parquet", [
        {"k1": "a", "k2": 1, "v": "x"},
        {"k1": "b", "k2": 2, "v": None},
    ])
    batch = load_source({"format": "parquet", "path": str(path)},
                        key_columns=("k1", "k2"))
    assert len(batch.rows) == 2
    assert batch.rows[1].values["v"] is None
    assert batch.columns == ("k1", "k2", "v")


def test_non_scalar_rejected():
    with pytest.raises(SourceFormatError) as ei:
        load_source({"format": "records", "records": [{"k1": "a", "nested": {"x": 1}}]},
                    key_columns=("k1",))
    assert ei.value.details == {"rownum": 1, "column": "nested", "type": "dict"}


def test_missing_file_and_relative_path_rejected():
    with pytest.raises(SourceFormatError) as ei:
        load_source({"format": "parquet", "path": "/no/such/file.parquet"},
                    key_columns=("k1",))
    assert "does not exist" in ei.value.args[0]

    with pytest.raises(SourceFormatError):
        load_source({"format": "ndjson", "path": "relative.jsonl"}, key_columns=("k1",))


def test_unknown_format():
    with pytest.raises(SourceFormatError):
        load_source({"format": "csv", "records": []}, key_columns=("k1",))
