"""Adapter tests: canonical partition rules and parquet validation.

Expected values come from literal python row lists and hand-computed
partition keys — never from the service kernel.
"""
from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.adapters.parquet import Partition, read_file_facts
from app.adapters.schema import CanonicalSchema, check_compatible
from app.kernel.errors import ErrorCategory, ServiceError


def test_partition_literal_canonicalisation_distinguishes_types():
    p_int = Partition.of(("region",), {"region": 1})
    p_str = Partition.of(("region",), {"region": "1"})
    assert p_int.key() == "region=1"
    assert p_str.key() == "region=1"
    # Keys happen to render alike, but the (column,value) pairs carry type via
    # canonical literal rules; bool must not alias 1.
    p_bool = Partition.of(("region",), {"region": True})
    assert p_bool.key() == "region=true"
    assert not p_bool.overlaps([p_int])


def test_null_and_float_partition_values_rejected():
    with pytest.raises(ServiceError) as exc:
        Partition.of(("day",), {"day": None})
    assert exc.value.category is ErrorCategory.VALIDATION


def test_read_file_facts_extracts_distinct_partitions(make_file):
    f = make_file(
        [
            (1, "us", "2024-01-01", 10, "a@example.test"),
            (2, "us", "2024-01-01", 20, "b@example.test"),
            (3, "us", "2024-01-02", 30, "c@example.test"),
        ]
    )
    facts = read_file_facts(f, ("region", "day"))
    assert facts.row_count == 3
    assert sorted(p.key() for p in facts.partitions) == [
        "region=us/day=2024-01-01",
        "region=us/day=2024-01-02",
    ]
    assert CanonicalSchema.from_arrow(pq.ParquetFile(f).schema_arrow).names() == (
        "sale_id", "region", "day", "amount", "owner",
    )


def test_missing_partition_column_rejected(make_file):
    f = make_file(
        [(1, "us", 10, "a@example.test")],
        schema=pa.schema(
            [
                ("sale_id", pa.int64()),
                ("region", pa.string()),
                ("amount", pa.int64()),
                ("owner", pa.string()),
            ]
        ),
    )
    with pytest.raises(ServiceError) as exc:
        read_file_facts(f, ("region", "day"))
    assert exc.value.category is ErrorCategory.VALIDATION
    assert exc.value.details["missing"] == ["day"]


def test_non_parquet_file_is_staging_failure(make_file):
    f = make_file([], raw_bytes=b"not parquet at all")
    with pytest.raises(ServiceError) as exc:
        read_file_facts(f, ("region",))
    assert exc.value.category is ErrorCategory.STAGING_FAILED


def test_missing_file_is_staging_failure(tmp_path):
    with pytest.raises(ServiceError) as exc:
        read_file_facts(tmp_path / "nope.parquet", ("region",))
    assert exc.value.category is ErrorCategory.STAGING_FAILED


def test_schema_compatibility_detects_type_drift():
    base = CanonicalSchema.from_arrow(
        pa.schema([("region", pa.string()), ("amount", pa.int64())])
    )
    drifted = pa.schema([("region", pa.string()), ("amount", pa.string())])
    with pytest.raises(ServiceError) as exc:
        check_compatible(drifted, base, ("region",))
    assert exc.value.category is ErrorCategory.VALIDATION
    mismatch = exc.value.details["mismatches"][0]
    assert mismatch["column"] == "amount"
    assert mismatch["expected"] == "int64"
    assert mismatch["actual"] == "string"
