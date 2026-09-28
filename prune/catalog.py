"""SQLite metadata store with transactional refresh.

All catalog updates run inside a single deferred transaction: a refresh that
fails midway rolls back and leaves the previous catalog intact. Stats are
serialized canonically (timestamps as epoch microseconds) so stored JSON never
depends on host locale or datetime repr.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List

from . import values as V
from .config import TableSpec
from .kernel import (Column, ColumnStat, FileStat, PartitionInfo, TableContext,
                     STATS_SCHEMA_VERSION)
from .parquet_adapter import discover_files, read_file_stat, ADAPTER_VERSION
from .transforms import TRANSFORM_SPEC_VERSION, tzdb_version

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tables_meta(
  name TEXT PRIMARY KEY,
  transform_json TEXT,
  transform_version TEXT,
  tzdb_version TEXT,
  adapter_version TEXT,
  stats_schema_version TEXT,
  refreshed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS columns_meta(
  table_name TEXT NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL,
  ordinal INTEGER NOT NULL, PRIMARY KEY(table_name, name));
CREATE TABLE IF NOT EXISTS partitions(
  table_name TEXT NOT NULL, label TEXT NOT NULL, is_null INTEGER NOT NULL,
  PRIMARY KEY(table_name, label));
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY, table_name TEXT NOT NULL, partition_label TEXT NOT NULL,
  num_rows INTEGER NOT NULL, size_bytes INTEGER NOT NULL, row_groups INTEGER NOT NULL,
  stats_version TEXT NOT NULL, pyarrow_version TEXT NOT NULL, stats_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS files_tbl ON files(table_name);
"""


def _encode_stat(v):
    if isinstance(v, _dt.datetime):
        return {"__us__": V.to_epoch_us(v)}
    if isinstance(v, _dt.date):
        return {"__date__": v.isoformat()}
    return v


def _decode_stat(v):
    if isinstance(v, dict) and "__us__" in v:
        return V.epoch_us_to_datetime(int(v["__us__"]))
    if isinstance(v, dict) and "__date__" in v:
        return v["__date__"]
    return v


def _stats_to_json(stats: Dict[str, ColumnStat]) -> str:
    out = {}
    for name, cs in stats.items():
        out[name] = {
            "present": cs.present,
            "min": _encode_stat(cs.minimum),
            "max": _encode_stat(cs.maximum),
            "null_count": cs.null_count,
            "min_truncated": cs.min_truncated,
            "max_truncated": cs.max_truncated,
        }
    return json.dumps(out, separators=(",", ":"), sort_keys=True)


def _stats_from_json(blob: str) -> Dict[str, ColumnStat]:
    data = json.loads(blob)
    out = {}
    for name, s in data.items():
        out[name] = ColumnStat(
            present=s.get("present", True),
            minimum=_decode_stat(s.get("min")),
            maximum=_decode_stat(s.get("max")),
            null_count=s.get("null_count"),
            min_truncated=s.get("min_truncated", False),
            max_truncated=s.get("max_truncated", False),
        )
    return out


class Catalog:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version',?)",
            (str(SCHEMA_VERSION),))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def transaction(self):
        try:
            self._conn.execute("BEGIN")
            yield self._conn
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    # ------------------------------------------------------------------ write

    def refresh_table(self, spec: TableSpec) -> dict:
        """Scan the table directory and atomically replace its catalog rows."""
        labels = discover_files(spec.root, spec.null_label)
        columns = {c.name: Column(c.name, c.type) for c in spec.columns.values()}
        parts: List[PartitionInfo] = []
        nfiles = nrows = nbytes = 0

        with self.transaction() as conn:
            conn.execute("DELETE FROM files WHERE table_name=?", (spec.name,))
            conn.execute("DELETE FROM partitions WHERE table_name=?", (spec.name,))
            conn.execute("DELETE FROM columns_meta WHERE table_name=?", (spec.name,))
            conn.execute("DELETE FROM tables_meta WHERE name=?", (spec.name,))

            for ordinal, c in enumerate(spec.columns.values()):
                conn.execute(
                    "INSERT INTO columns_meta(table_name,name,type,ordinal) "
                    "VALUES(?,?,?,?)", (spec.name, c.name, c.type, ordinal))

            for label, files in labels.items():
                is_null = label == spec.null_label
                conn.execute(
                    "INSERT INTO partitions(table_name,label,is_null) VALUES(?,?,?)",
                    (spec.name, label, int(is_null)))
                for fp in files:
                    fs = read_file_stat(fp, columns)
                    conn.execute(
                        "INSERT INTO files(path,table_name,partition_label,num_rows,"
                        "size_bytes,row_groups,stats_version,pyarrow_version,stats_json)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (fs.path, spec.name, label, fs.num_rows, fs.size_bytes,
                         fs.row_groups, fs.stats_version, fs.pyarrow_version,
                         _stats_to_json(fs.stats)))
                    nfiles += 1
                    nrows += fs.num_rows
                    nbytes += fs.size_bytes

            tspec = {"kind": spec.transform.kind,
                     "source_column": spec.transform.source_column,
                     "tz": spec.transform.tz_name,
                     "null_label": spec.null_label} if spec.transform else None
            conn.execute(
                "INSERT INTO tables_meta(name,transform_json,transform_version,"
                "tzdb_version,adapter_version,stats_schema_version,refreshed_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (spec.name, json.dumps(tspec) if tspec else None,
                 TRANSFORM_SPEC_VERSION if tspec else None,
                 tzdb_version() if tspec else None,
                 ADAPTER_VERSION, STATS_SCHEMA_VERSION,
                 _dt.datetime.now(_dt.timezone.utc).isoformat()))

        return {"partitions": len(labels), "files": nfiles,
                "rows": nrows, "bytes": nbytes}

    # ------------------------------------------------------------------- read

    def table_names(self) -> List[str]:
        return [r["name"] for r in self._conn.execute(
            "SELECT name FROM tables_meta ORDER BY name")]

    def load_context(self, spec: TableSpec, name: str) -> TableContext:
        row = self._conn.execute(
            "SELECT * FROM tables_meta WHERE name=?", (name,)).fetchone()
        if row is None:
            raise KeyError(f"table {name!r} not in catalog; run refresh")

        cols = {}
        for r in self._conn.execute(
                "SELECT name,type FROM columns_meta WHERE table_name=? ORDER BY ordinal",
                (name,)):
            cols[r["name"]] = Column(r["name"], r["type"])

        tspec = json.loads(row["transform_json"]) if row["transform_json"] else None
        transform = None
        if tspec:
            from .transforms import MonthTransform
            transform = MonthTransform(source_column=tspec["source_column"],
                                       tz_name=tspec["tz"],
                                       null_label=tspec.get("null_label", "__null__"))

        parts: List[PartitionInfo] = []
        for pr in self._conn.execute(
                "SELECT label,is_null FROM partitions WHERE table_name=? ORDER BY label",
                (name,)):
            files = []
            for fr in self._conn.execute(
                    "SELECT * FROM files WHERE table_name=? AND partition_label=? "
                    "ORDER BY path", (name, pr["label"])):
                files.append(FileStat(
                    path=fr["path"], num_rows=fr["num_rows"],
                    size_bytes=fr["size_bytes"], row_groups=fr["row_groups"],
                    stats=_stats_from_json(fr["stats_json"]),
                    stats_version=fr["stats_version"],
                    pyarrow_version=fr["pyarrow_version"]))
            parts.append(PartitionInfo(label=pr["label"],
                                       is_null=bool(pr["is_null"]), files=files))

        return TableContext(
            name=name, columns=cols, transform=transform, partitions=parts,
            recorded_transform_version=row["transform_version"],
            recorded_tzdb_version=row["tzdb_version"])
