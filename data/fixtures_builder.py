"""Synthetic fixture builder (local, deterministic).

Generates immutable parquet files under ``data/fixtures/<table>`` plus an
``expected.json`` oracle.  The oracle values are computed independently here
from the raw python rows (row counts are ``len(rows)`` constants) — the core
service under test is never used to produce expected answers.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "data" / "fixtures"

# ---------------------------------------------------------------------------
# Ground-truth rows, authored by hand. Columns:
#   sale_id:int64, region:str, day:str, amount:int64, owner:str
# `owner` is a sensitive column — tests assert it never appears in diagnostics.
# ---------------------------------------------------------------------------

ROWS_US_20240101 = [
    (1, "us", "2024-01-01", 10, "alice@example.test"),
    (2, "us", "2024-01-01", 20, "alice@example.test"),
    (3, "us", "2024-01-01", 30, "bob@example.test"),
]
ROWS_US_20240102 = [
    (4, "us", "2024-01-02", 40, "bob@example.test"),
    (5, "us", "2024-01-02", 50, "carol@example.test"),
]
ROWS_EU_20240101 = [
    (6, "eu", "2024-01-01", 60, "dave@example.test"),
    (7, "eu", "2024-01-01", 70, "dave@example.test"),
]
# Overwrite replacement for us/2024-01-01 — two rows, amount reset.
ROWS_US_20240101_V2 = [
    (101, "us", "2024-01-01", 100, "erin@example.test"),
    (102, "us", "2024-01-01", 200, "erin@example.test"),
]

SCHEMA = pa.schema(
    [
        ("sale_id", pa.int64()),
        ("region", pa.string()),
        ("day", pa.string()),
        ("amount", pa.int64()),
        ("owner", pa.string()),
    ]
)

FILE_SPECS = [
    ("events", "us_20240101.parquet", ROWS_US_20240101),
    ("events", "us_20240102.parquet", ROWS_US_20240102),
    ("events", "eu_20240101.parquet", ROWS_EU_20240101),
    ("events", "us_20240101_v2.parquet", ROWS_US_20240101_V2),
]


def _write_table(path: Path, rows: list[tuple]) -> None:
    columns = list(zip(*rows)) if rows else [(), (), (), (), ()]
    table = pa.table(
        {
            "sale_id": pa.array(columns[0], type=pa.int64()),
            "region": pa.array(columns[1], type=pa.string()),
            "day": pa.array(columns[2], type=pa.string()),
            "amount": pa.array(columns[3], type=pa.int64()),
            "owner": pa.array(columns[4], type=pa.string()),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="snappy")


def _write_bad_files(root: Path) -> None:
    bad_dir = root / "bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    # 1. Not a parquet file at all.
    (bad_dir / "not_parquet.parquet").write_bytes(b"this is definitely not parquet")
    # 2. Parquet with an incompatible schema (amount is a string).
    bad = pa.table(
        {
            "sale_id": pa.array([999], pa.int64()),
            "region": pa.array(["us"], pa.string()),
            "day": pa.array(["2024-01-01"], pa.string()),
            "amount": pa.array(["forty-two"], pa.string()),
            "owner": pa.array(["mallory@example.test"], pa.string()),
        }
    )
    pq.write_table(bad, bad_dir / "wrong_schema.parquet")
    # 3. Parquet missing a partition column (no `day`).
    missing = pa.table(
        {
            "sale_id": pa.array([998], pa.int64()),
            "region": pa.array(["us"], pa.string()),
            "amount": pa.array([1], pa.int64()),
            "owner": pa.array(["mallory@example.test"], pa.string()),
        }
    )
    pq.write_table(missing, bad_dir / "missing_partition_col.parquet")


def _write_orphan_dir(root: Path) -> str:
    """A physical directory+file unrelated to any table (orphan fixture)."""
    orphan_dir = root.parent / "_orphan_demo"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    stray = orphan_dir / "stray.parquet"
    t = pa.table({"x": pa.array([1, 2, 3], pa.int64())})
    pq.write_table(t, stray)
    return os.path.relpath(stray, root.parent)


def build(root: Path | None = None) -> dict:
    root = root or FIXTURE_ROOT
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    orphan = root.parent / "_orphan_demo"
    if orphan.exists():
        shutil.rmtree(orphan)

    expected_files: dict[str, dict] = {}
    for table, name, rows in FILE_SPECS:
        path = root / table / name
        _write_table(path, rows)
        # Independent oracle: literal len() of the authored rows.
        partitions = sorted({f"region={r[1]}/day={r[2]}" for r in rows})
        expected_files[f"{table}/{name}"] = {
            "row_count": len(rows),
            "partitions": partitions,
            "amount_sum": sum(r[3] for r in rows),
        }

    _write_bad_files(root)
    stray_rel = _write_orphan_dir(root)

    # Expected end-state of the documented demo flow, hand-derived:
    #  1. append us_20240101 (3), us_20240102 (2), eu_20240101 (2) -> 7 rows
    #  2. overwrite us/day=2024-01-01 with v2 -> 3 old rows gone, 2 new rows
    expected = {
        "table": "events",
        "partition_spec": ["region", "day"],
        "schema_columns": ["sale_id", "region", "day", "amount", "owner"],
        "files": expected_files,
        "after_append_all": {
            "row_count": 3 + 2 + 2,
            "partitions": [
                "region=eu/day=2024-01-01",
                "region=us/day=2024-01-01",
                "region=us/day=2024-01-02",
            ],
        },
        "after_overwrite_us_0101": {
            "row_count": 2 + 2 + 2,
            "amount_sum": (100 + 200) + 40 + 50 + 60 + 70,
            "partitions": [
                "region=eu/day=2024-01-01",
                "region=us/day=2024-01-01",
                "region=us/day=2024-01-02",
            ],
            "files_for_partition": {
                "region=us/day=2024-01-01": ["events/us_20240101_v2.parquet"]
            },
        },
        "orphan_fixture_relpath": stray_rel,
        "bad_files": [
            "bad/not_parquet.parquet",
            "bad/wrong_schema.parquet",
            "bad/missing_partition_col.parquet",
        ],
    }
    (root / "expected.json").write_text(
        json.dumps(expected, indent=2, sort_keys=True), encoding="utf-8"
    )
    return expected


if __name__ == "__main__":
    result = build()
    print(json.dumps(result, indent=2, sort_keys=True))
