"""Shared pytest fixtures: build the synthetic dataset and a fresh catalog.

The fixtures are regenerated per test session under tests/_workdir. Nothing is
shared with the hand-generated data/ directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from prune.catalog import Catalog
from prune.config import Config, TableSpec
from prune.transforms import MonthTransform
from scripts.make_fixtures import build as build_fixtures
from prune.parquet_adapter import pa_type_to_domain  # noqa: F401
import pyarrow.parquet as pq


@pytest.fixture(scope="session")
def workdir(tmp_path_factory):
    return tmp_path_factory.mktemp("prune")


@pytest.fixture(scope="session")
def populated(workdir):
    data_root = workdir / "events"
    info = build_fixtures(data_root)
    spec = TableSpec(
        name="events", root=data_root,
        columns={
            "id": _col("id", "int"),
            "ts": _col("ts", "datetime"),
            "name": _col("name", "str"),
            "amount": _col("amount", "int"),
        },
        transform=MonthTransform(source_column="ts", tz_name="Asia/Shanghai"),
        null_label="__null__")
    db = workdir / "catalog.db"
    with Catalog(db) as cat:
        summary = cat.refresh_table(spec)
        ctx = cat.load_context(spec, "events")
    cfg = Config(catalog_db=db, base_dir=workdir, tables={"events": spec})
    return {"config": cfg, "catalog_db": db, "spec": spec, "ctx": ctx,
            "summary": summary, "root": data_root}


def _col(name, type_):
    from prune.kernel import Column
    return Column(name, type_)


@pytest.fixture
def catalog_db(populated):
    return populated["catalog_db"]


@pytest.fixture
def config(populated):
    return populated["config"]


@pytest.fixture
def ctx(populated):
    # reload so per-test catalog edits never leak
    with Catalog(populated["catalog_db"]) as cat:
        yield cat.load_context(populated["spec"], "events")


@pytest.fixture
def domains(populated):
    return {c: col.type for c, col in populated["ctx"].columns.items()}


@pytest.fixture
def all_file_paths(populated):
    return sorted(str(p) for p in populated["root"].rglob("*.parquet"))
