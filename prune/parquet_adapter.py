"""Parquet format adapter.

Isolates all PyArrow-specific knowledge from the kernel: directory discovery,
row-group statistics aggregation, and the row-level full scan used by the
independent reference checker. The kernel only sees `kernel.FileStat` /
`ColumnStat` values.

Truncation semantics (Parquet): a writer may record truncated UTF8 min/max in
the *legacy* statistics fields. `Statistics.min_raw`/`max_raw` read those
fields; `min`/`max` prefer the modern min_value/max_value fields which must not
be truncated. When only a legacy value is available (or it differs from the
modern one), the stat is marked truncated so the kernel treats it as an
uncertain bound rather than a tight one.

Optional sidecar ``<file>.stats-override.json`` lets integration tests emulate
foreign writers that emit truncated / missing stats without replacing PyArrow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq

from . import values as V
from .kernel import Column, ColumnStat, FileStat, STATS_SCHEMA_VERSION

ADAPTER_NAME = "parquet"
ADAPTER_VERSION = "parquet-adapter-v1"

_PA_TO_DOMAIN = {
    pa.int8(): V.INT, pa.int16(): V.INT, pa.int32(): V.INT, pa.int64(): V.INT,
    pa.uint8(): V.INT, pa.uint16(): V.INT, pa.uint32(): V.INT,
    pa.uint64(): V.INT,
    pa.float16(): V.FLOAT, pa.float32(): V.FLOAT, pa.float64(): V.FLOAT,
    pa.string(): V.STR, pa.large_string(): V.STR,
    pa.bool_(): V.BOOL,
}


class AdapterError(RuntimeError):
    pass


def pa_type_to_domain(pa_type: pa.DataType) -> Optional[str]:
    if pa.types.is_timestamp(pa_type):
        return V.DATETIME
    if pa.types.is_date(pa_type):
        return V.DATE
    for cand, dom in _PA_TO_DOMAIN.items():
        if pa_type == cand:
            return dom
    return None


def _norm(value, domain: str):
    if value is None:
        return None
    try:
        return V.canonical(value, domain)
    except ValueError:
        return None


def discover_files(table_root: Path, null_label: str) -> Dict[str, List[Path]]:
    """Map partition label -> parquet files.

    Layout: ``<root>/<partition-label>/<file>.parquet`` (one directory level).
    Labels are taken from directory names *verbatim* — they are partition
    values produced by the writer, never recomputed from row data.
    """
    root = Path(table_root)
    if not root.exists():
        raise AdapterError(f"table root does not exist: {root}")
    out: Dict[str, List[Path]] = {}
    for part_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        files = sorted(part_dir.glob("*.parquet"))
        if files:
            out[part_dir.name] = files
    return out


def _stat_value(stats, attr: str, domain: str):
    """Return (canonical value, truncated flag) from a row-group Statistics.

    ``min``/``max`` read the modern min_value/max_value fields; ``min_raw``/
    ``max_raw`` read the LEGACY fields, which expose the *physical* encoding
    (bytes for BYTE_ARRAY, int for timestamps) and so must be decoded before
    any comparison. Only UTF8 legacy stats may be truncated per the Parquet
    spec, so the flag is restricted to the string domain and raised only when
    the decoded legacy value differs from the modern one (or the modern field
    is absent while a legacy one is present).
    """
    modern = getattr(stats, attr)
    raw = getattr(stats, attr + "_raw", modern)
    val = modern if modern is not None else raw
    truncated = False
    if domain == V.STR and val is not None:
        legacy = raw
        if isinstance(legacy, (bytes, bytearray)):
            try:
                legacy = bytes(legacy).decode("utf-8")
            except UnicodeDecodeError:
                # A legacy prefix that splits a code point IS truncation.
                legacy = None
                truncated = True
        if not truncated:
            if modern is None and legacy is not None:
                truncated = True
            elif modern is not None and legacy is not None and legacy != modern:
                truncated = True
    return _norm(val, domain), bool(truncated)


def read_file_stat(path: Path, columns: Dict[str, Column]) -> FileStat:
    pf = pq.ParquetFile(path)
    schema = pf.schema_arrow
    present = {name: pa_type_to_domain(schema.field(name).type)
               for name in schema.names}

    colstats: Dict[str, ColumnStat] = {}
    rg_count = pf.metadata.num_row_groups
    total_rows = pf.metadata.num_rows

    # per-column aggregation state over row groups
    agg: Dict[str, dict] = {}
    for name in columns:
        if name not in present:
            colstats[name] = ColumnStat(present=False)
            continue
        agg[name] = {"min": None, "max": None, "nulls": 0,
                     "null_known": True, "min_tr": False, "max_tr": False,
                     "seen_nonnull": False}

    for rg in range(rg_count):
        rgm = pf.metadata.row_group(rg)
        for ci in range(rgm.num_columns):
            cc = rgm.column(ci)
            name = cc.path_in_schema
            if name not in agg:
                continue
            st = cc.statistics
            a = agg[name]
            if st is None:
                # Whole row group without statistics: nothing is provable.
                a["min"] = a["max"] = "__unavailable__"
                a["null_known"] = False
                continue
            if st.has_null_count:
                a["nulls"] += st.null_count
            else:
                a["null_known"] = False
            if st.has_min_max:
                domain = present[name]
                mn, mntr = _stat_value(st, "min", domain)
                mx, mxtr = _stat_value(st, "max", domain)
                if mn is not None:
                    a["seen_nonnull"] = True
                    a["min"] = min_like(a["min"], mn)
                if mx is not None:
                    a["max"] = max_like(a["max"], mx)
                a["min_tr"] = a["min_tr"] or mntr
                a["max_tr"] = a["max_tr"] or mxtr
            else:
                a["min"] = a["max"] = "__unavailable__"

    for name, a in agg.items():
        if a["min"] == "__unavailable__" or not a["seen_nonnull"]:
            mn = mx = None
        else:
            mn, mx = a["min"], a["max"]
        colstats[name] = ColumnStat(
            minimum=mn, maximum=mx,
            null_count=a["nulls"] if a["null_known"] else None,
            min_truncated=a["min_tr"] and mn is not None,
            max_truncated=a["max_tr"] and mx is not None,
            present=True)

    fs = FileStat(
        path=str(path),
        num_rows=total_rows,
        size_bytes=path.stat().st_size,
        row_groups=rg_count,
        stats=colstats,
        stats_version=STATS_SCHEMA_VERSION,
        pyarrow_version=pa.__version__,
    )
    _apply_override(path, fs, columns)
    return fs


def min_like(a, b):
    if a is None:
        return b
    if b is None:
        return a
    try:
        return min(a, b)
    except TypeError:
        return a


def max_like(a, b):
    if a is None:
        return b
    if b is None:
        return a
    try:
        return max(a, b)
    except TypeError:
        return a


def _apply_override(path: Path, fs: FileStat, columns: Dict[str, Column]) -> None:
    side = path.with_suffix(path.suffix + ".stats-override.json")
    if not side.exists():
        return
    data = json.loads(side.read_text())
    for name, spec in data.items():
        if name not in columns:
            continue
        dom = columns[name].type
        cs = fs.stats.setdefault(name, ColumnStat())
        object.__setattr__(cs, "present", spec.get("present", True))
        if "min" in spec:
            object.__setattr__(cs, "minimum", _norm(spec["min"], dom))
        if "max" in spec:
            object.__setattr__(cs, "maximum", _norm(spec["max"], dom))
        if "null_count" in spec:
            object.__setattr__(cs, "null_count", spec["null_count"])
        if "min_truncated" in spec:
            object.__setattr__(cs, "min_truncated", bool(spec["min_truncated"]))
        if "max_truncated" in spec:
            object.__setattr__(cs, "max_truncated", bool(spec["max_truncated"]))
    fs.stats_version = data.get("__stats_version__", fs.stats_version)


# ---------------------------------------------------------------------------
# Row-level scan (used only by the independent reference checker)
# ---------------------------------------------------------------------------

def scan_rows(path: Path) -> pa.Table:
    return pq.read_table(path)


def write_parquet(table: pa.Table, path: Path, write_statistics: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, write_statistics=write_statistics)
