"""Catalog transaction + config tests, including rollback atomicity and the
version-mismatch guard that forces a conservative plan.
"""

from __future__ import annotations

import pytest

from prune.catalog import Catalog, SCHEMA_VERSION
from prune.config import Config, TableSpec, load_config, ConfigError
from prune.kernel import Column, plan_prune
from prune.transforms import MonthTransform, tzdb_version
from prune.models import parse_predicate
from scripts.make_fixtures import build
from pathlib import Path


COLS = {n: Column(n, t) for n, t in
        [("id", "int"), ("ts", "datetime"), ("name", "str"), ("amount", "int")]}


def test_refresh_is_atomic_on_failure(workdir):
    root = workdir / "ev_atomic"
    build(root)
    spec = TableSpec("ev_atomic", root, COLS,
                     MonthTransform("ts", "Asia/Shanghai"), "__null__")
    db = workdir / "atomic.db"
    with Catalog(db) as cat:
        cat.refresh_table(spec)
        before = cat.table_names()
        assert before == ["ev_atomic"]

        # Force a failure mid-refresh by pointing at a missing root; the
        # transaction must roll back and the prior snapshot must survive.
        bad = TableSpec("ev_atomic", root / "does-not-exist", COLS,
                        MonthTransform("ts", "Asia/Shanghai"), "__null__")
        with pytest.raises(Exception):
            cat.refresh_table(bad)

        ctx = cat.load_context(spec, "ev_atomic")
        assert sum(len(p.files) for p in ctx.partitions) == 10
        assert ctx.recorded_tzdb_version == tzdb_version()


def test_stored_stats_roundtrip_timestamps(workdir):
    root = workdir / "ev_rt"
    build(root)
    spec = TableSpec("ev_rt", root, COLS,
                     MonthTransform("ts", "Asia/Shanghai"), "__null__")
    db = workdir / "rt.db"
    with Catalog(db) as cat:
        cat.refresh_table(spec)
        ctx = cat.load_context(spec, "ev_rt")
    mar = next(p for p in ctx.partitions if p.label == "2024-03")
    early = next(f for f in mar.files if f.path.endswith("mar_early.parquet"))
    cs = early.stats["ts"]
    # canonical UTC datetime survives the SQLite JSON round-trip
    assert cs.minimum.tzinfo is not None
    assert cs.maximum.year == 2024 and cs.maximum.month == 3
    assert cs.null_count == 0


def test_version_mismatch_disables_partition_pruning(workdir):
    root = workdir / "ev_vm"
    build(root)
    spec = TableSpec("ev_vm", root, COLS,
                     MonthTransform("ts", "Asia/Shanghai"), "__null__")
    db = workdir / "vm.db"
    with Catalog(db) as cat:
        cat.refresh_table(spec)
        # tamper the recorded tzdb version, as if a different package were
        # installed at query time
        with cat.transaction() as conn:
            conn.execute(
                "UPDATE tables_meta SET tzdb_version=? WHERE name=?",
                ("tzdata1970.1", "ev_vm"))
        ctx = cat.load_context(spec, "ev_vm")

    pred = parse_predicate({"op": "IS_NULL", "column": "ts"})
    plan = plan_prune(ctx, pred, "r")
    # nothing may be pruned when versions disagree
    assert all(p.verdict != "PRUNED" for p in plan.partitions)
    assert any(f["code"] == "TRANSFORM_VERSION_MISMATCH"
               for f in plan.failures)


def test_config_rejects_bad_column_type(tmp_path):
    cfg = tmp_path / "bad.toml"
    cfg.write_text(
        'base_dir="."\ncatalog_db="c.db"\n'
        '[[tables]]\nname="t"\npath="t"\n'
        '[[tables.columns]]\nname="x"\ntype="weird"\n')
    with pytest.raises(ConfigError):
        load_config(cfg)


def test_config_rejects_partition_on_non_datetime(tmp_path):
    cfg = tmp_path / "bad2.toml"
    cfg.write_text(
        'base_dir="."\ncatalog_db="c.db"\n'
        '[[tables]]\nname="t"\npath="t"\n'
        '[[tables.columns]]\nname="x"\ntype="int"\n'
        '[tables.partition]\nkind="month_tz"\nsource_column="x"\ntz="UTC"\n')
    with pytest.raises(ConfigError):
        load_config(cfg)
