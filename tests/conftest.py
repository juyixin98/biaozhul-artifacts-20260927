"""Shared pytest fixtures: isolated storage per test, synthetic parquet files.

Every test gets its own temp warehouse / staging / sqlite dirs — no test can
read state produced by another.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.container import Container  # noqa: E402
from config.settings import Settings  # noqa: E402


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        warehouse_dir=tmp_path / "warehouse",
        db_path=tmp_path / "run_results" / "metadata.sqlite3",
        staging_dir=tmp_path / "run_results" / "staging",
        orphan_prefix="_orphan_demo",
        max_retry_attempts=5,
        log_full_paths=True,
        base_dir=tmp_path,
    )


@pytest.fixture()
def container(settings: Settings) -> Container:
    c = Container(settings)
    yield c
    c.close()


@pytest.fixture()
def service(container: Container):
    return container.service


@pytest.fixture()
def events_table(service):
    """Create the standard events table partitioned by (region, day)."""
    service.create_table("events", ["region", "day"])
    return "events"


def write_parquet(
    path: Path,
    rows: list[tuple],
    *,
    schema: pa.Schema | None = None,
) -> Path:
    """Write rows with the events schema (or a custom schema)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if schema is None:
        cols = list(zip(*rows)) if rows else [(), (), (), (), ()]
        table = pa.table(
            {
                "sale_id": pa.array(cols[0], type=pa.int64()),
                "region": pa.array(cols[1], type=pa.string()),
                "day": pa.array(cols[2], type=pa.string()),
                "amount": pa.array(cols[3], type=pa.int64()),
                "owner": pa.array(cols[4], type=pa.string()),
            }
        )
    else:
        cols = list(zip(*rows)) if rows else [tuple() for _ in schema]
        table = pa.table(
            {field.name: pa.array(cols[i], type=field.type)
             for i, field in enumerate(schema)},
            schema=schema,
        )
    pq.write_table(table, path)
    return path


@pytest.fixture()
def make_file(tmp_path: Path):
    counter = {"n": 0}

    def _make(
        rows: list[tuple],
        *,
        name: str | None = None,
        schema: pa.Schema | None = None,
        raw_bytes: bytes | None = None,
    ) -> Path:
        counter["n"] += 1
        if name is not None:
            path = tmp_path / name
        else:
            path = tmp_path / f"input_{counter['n']}.parquet"
        if raw_bytes is not None:
            path.write_bytes(raw_bytes)
        else:
            write_parquet(path, rows, schema=schema)
        return path

    return _make
