"""Parquet IO tests: page boundaries, self/Arrow interop, error locations."""
import os
import tempfile

import pyarrow.parquet as pq
import pytest

from app.format import parquet_io
from app.format.parquet_io import ParquetReadError
from app.kernel.levels import encode_records
from app.kernel.schema import build_schema, leaf_columns

from .fixtures import (
    LIST_OF_STRUCT_RECORDS, NESTED_LIST_RECORDS, PRIMITIVE_RECORDS,
    SCHEMA_LIST_OF_STRUCT, SCHEMA_NESTED_LIST, SCHEMA_PRIMITIVES,
    cross_page_records,
)


@pytest.fixture
def tmp_parquet(tmp_path):
    return str(tmp_path / "f.parquet")


def test_self_write_read_nested(tmp_parquet):
    root = build_schema(SCHEMA_NESTED_LIST)
    parquet_io.write_file(tmp_parquet, root, NESTED_LIST_RECORDS)
    _, back, _ = parquet_io.read_file_records(tmp_parquet)
    assert back == NESTED_LIST_RECORDS


def test_pyarrow_reads_our_file_with_nested_schema(tmp_parquet):
    root = build_schema(SCHEMA_NESTED_LIST)
    parquet_io.write_file(tmp_parquet, root, NESTED_LIST_RECORDS)
    table = pq.read_table(tmp_parquet)
    # Arrow must recognise ids as a 2-level list, not a flat binary column.
    assert str(table.schema.field("ids").type).startswith("list<list:")
    assert table.to_pylist() == NESTED_LIST_RECORDS


def test_we_read_pyarrow_written_file(tmp_parquet):
    from app.oracle import write_reference_parquet
    root = build_schema(SCHEMA_LIST_OF_STRUCT)
    write_reference_parquet(tmp_parquet, root, LIST_OF_STRUCT_RECORDS)
    _, back, _ = parquet_io.read_file_records(tmp_parquet)
    assert back == LIST_OF_STRUCT_RECORDS


def test_cross_page_records_reassemble(tmp_parquet):
    root = build_schema(SCHEMA_NESTED_LIST)
    records = cross_page_records(300)
    # force a boundary every 3 records; records vary in width enormously,
    # including a 3-element inner list spanning many leaf slots.
    parquet_io.write_file(tmp_parquet, root, records,
                          force_page_after_records=3)
    root_b, cols, n = parquet_io.read_file_events(tmp_parquet)
    for col in cols:
        # Every page's record counts must sum to the total; no page splits a
        # record, so page boundaries never orphan nested children.
        assert sum(p.record_count for p in col.pages) == n
    _, back, _ = parquet_io.read_file_records(tmp_parquet)
    assert back == records
    # And there really was more than one page.
    total_pages = sum(len(c.pages) for c in cols)
    assert total_pages > 1


def test_page_never_splits_wide_record(tmp_parquet):
    root = build_schema(SCHEMA_NESTED_LIST)
    wide = {"id": 1,
            "ids": [[j, j + 1, j + 2] for j in range(50)],
            "tags": [f"tag-{j}" for j in range(50)]}
    # Tiny target; the wide record must still land whole in one page.
    parquet_io.write_file(tmp_parquet, root, [wide], page_size_bytes=1)
    _, cols, _ = parquet_io.read_file_events(tmp_parquet)
    for col in cols:
        assert len(col.pages) == 1
    _, back, _ = parquet_io.read_file_records(tmp_parquet)
    assert back == [wide]


def test_primitives_all_types_roundtrip(tmp_parquet):
    root = build_schema(SCHEMA_PRIMITIVES)
    parquet_io.write_file(tmp_parquet, root, PRIMITIVE_RECORDS)
    _, back, _ = parquet_io.read_file_records(tmp_parquet)
    for exp, got in zip(PRIMITIVE_RECORDS, back):
        assert got["id"] == exp["id"]
        assert got["b"] == exp["b"]
        assert got["s"] == exp["s"]
        assert got["f64"] == exp["f64"]
        assert got["i32"] == exp["i32"]
        if exp["f32"] is None:
            assert got["f32"] is None
        else:
            assert abs(got["f32"] - exp["f32"]) < 1e-6


def test_unsupported_physical_byte_array_rejected_on_read(tmp_parquet):
    # Hand-craft a footer declaring BYTE_ARRAY without STRING by writing a
    # string file then corrupting its converted_type is complex; instead verify
    # direct schema guard through reading an Arrow BINARY (non-string) file.
    import pyarrow as pa
    import pyarrow.parquet as pqp
    table = pa.table({"raw": pa.array([b"\x00\x01", b"\x02"], pa.binary())})
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "b.parquet")
        pqp.write_table(table, p, data_page_version="1.0",
                        use_dictionary=False, write_statistics=False)
        with pytest.raises(ParquetReadError) as exc:
            parquet_io.read_file_records(p)
        assert "BYTE_ARRAY without STRING" in str(exc.value)


def test_page_layout_reported_with_page_numbers(tmp_parquet):
    root = build_schema(SCHEMA_NESTED_LIST)
    parquet_io.write_file(tmp_parquet, root, NESTED_LIST_RECORDS,
                          force_page_after_records=2)
    _, cols, _ = parquet_io.read_file_events(tmp_parquet)
    ids_col = next(c for c in cols if c.leaf.path[0] == "ids")
    pages = ids_col.pages
    assert pages[0].page_index == 0
    assert pages[0].first_record_index == 0
    # first page covers records 0 and 1 (NULL list and empty list -> 1 slot each)
    assert pages[0].record_count == 2
    # record indices must be contiguous
    starts = [p.first_record_index for p in pages]
    counts = [p.record_count for p in pages]
    reconstructed = []
    for s, c in zip(starts, counts):
        assert s == len(reconstructed)
        reconstructed.extend(range(s, s + c))
    assert reconstructed == list(range(sum(counts)))
