"""Generate the local synthetic fixture set (deterministic, no network).

Layout (all timestamps are given in Asia/Shanghai wall-clock; the writer stores
true UTC instants; partition directories are month labels in Asia/Shanghai):

    data/events/
      1969-12/  neg_a.parquet, neg_b.parquet     # negative epochs
      2024-02/  feb_real.parquet                 # ordinary February rows
      2024-03/  mar_early.parquet               # spans Mar 04..Mar 05
                mar_mid.parquet                 # Mar 10..Mar 11  (file-prunable)
                mar_boundary.parquet            # Mar 31..Apr 01 (file-prunable)
      2024-04/  apr.parquet
      2024-05/  may_truncstr.parquet            # long shared-prefix strings
                + .stats-override.json          # emulates a foreign writer
                                                # that truncated UTF8 max stats
      __null__/ null_ts.parquet                 # all ts NULL, normal stats
                  no_stats.parquet              # statistics block disabled

The writer routes rows by the forward transform: non-null timestamps go to a
month directory, NULL timestamps ALWAYS go to the explicit NULL label. The
file-level stat layer then distinguishes, within the null directory, files
that have usable null counts from files whose statistics are missing.
``id`` is globally unique and is the ground-truth row identity.
"""

from __future__ import annotations

import json
import sys
import datetime as _dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow as pa

from prune.parquet_adapter import write_parquet

SH = "+08:00"
TS = pa.timestamp("us", tz="UTC")


def _ts(v):
    if v is None:
        return None
    s = v[:-1] + "+00:00" if v.endswith("Z") else v
    d = _dt.datetime.fromisoformat(s)
    return d.astimezone(_dt.timezone.utc)


def rows(ids, tss, names, amounts):
    return pa.table({
        "id": pa.array(ids, type=pa.int64()),
        "ts": pa.array([_ts(t) for t in tss], type=TS),
        "name": pa.array(names, type=pa.string()),
        "amount": pa.array(amounts, type=pa.int64()),
    })


def build(root: Path) -> dict:
    root = Path(root)
    written = []

    def put(part: str, fname: str, table: pa.Table, stats: bool = True,
            override: dict | None = None):
        p = root / part / fname
        write_parquet(table, p, write_statistics=stats)
        written.append(str(p))
        if override is not None:
            side = p.with_suffix(p.suffix + ".stats-override.json")
            side.write_text(json.dumps(override, indent=2))
            written.append(str(side))

    # --- negative epoch: Dec 1969, Shanghai (UTC instants in the same month) -
    put("1969-12", "neg_a.parquet", rows(
        ids=[1001, 1002],
        tss=[f"1969-12-15T09:00:00{SH}", f"1969-12-15T23:00:00{SH}"],
        names=["neg-a1", "neg-a2"], amounts=[-10, -11]))
    put("1969-12", "neg_b.parquet", rows(
        ids=[1003, 1004],
        tss=[f"1969-12-01T00:30:00{SH}", f"1969-12-31T23:30:00{SH}"],
        names=["neg-b1", "neg-b2"], amounts=[-12, -13]))

    # --- Feb 2024: ordinary in-month rows; amount column entirely NULL ------
    # Exercises FILE-level handling on a NON-partition column (ts range still
    # prunes this directory at level 1; an amount predicate sees all-null).
    put("2024-02", "feb_real.parquet", rows(
        ids=[2000, 2001, 2002],
        tss=[f"2024-02-02T08:00:00{SH}", f"2024-02-14T10:00:00{SH}",
             f"2024-02-25T22:00:00{SH}"],
        names=["feb-1", "feb-2", "feb-3"], amounts=[None, None, None]))

    # --- March 2024: one kept file, two file-stat-prunable files ------------
    put("2024-03", "mar_early.parquet", rows(
        ids=[3001, 3002, 3003],
        tss=[f"2024-03-04T22:00:00{SH}", f"2024-03-05T00:00:00{SH}",
             f"2024-03-05T15:30:00{SH}"],
        names=["mar-early1", "mar-target1", "mar-target2"],
        amounts=[30, 31, 32]))
    put("2024-03", "mar_mid.parquet", rows(
        ids=[3011, 3012],
        tss=[f"2024-03-10T08:00:00{SH}", f"2024-03-11T20:00:00{SH}"],
        names=["mar-mid1", "mar-mid2"], amounts=[33, 34]))
    put("2024-03", "mar_boundary.parquet", rows(
        ids=[3021, 3022],
        tss=[f"2024-03-31T23:30:00{SH}", f"2024-04-01T00:30:00{SH}"],
        names=["mar-edge", "apr-edge"], amounts=[35, 36]))

    # --- April 2024 ----------------------------------------------------------
    put("2024-04", "apr.parquet", rows(
        ids=[4001, 4002],
        tss=[f"2024-04-10T08:00:00{SH}", f"2024-04-20T19:00:00{SH}"],
        names=["apr-1", "apr-2"], amounts=[40, 41]))

    # --- May 2024: truncated UTF8 max stats emulated via sidecar ------------
    # Real values share a long prefix; the (foreign) writer only persisted a
    # prefix as the max, flagged truncated. The sidecar reproduces exactly that
    # on-disk situation without swapping the Parquet engine.
    prefix = "z" * 60
    put("2024-05", "may_truncstr.parquet", rows(
        ids=[5001, 5002, 5003],
        tss=[f"2024-05-02T08:00:00{SH}", f"2024-05-02T12:00:00{SH}",
             f"2024-05-03T08:00:00{SH}"],
        names=[prefix + "001_tail_a", prefix + "002_tail_b", prefix + "003_tail_c"],
        amounts=[50, 51, 52]),
        override={
            "name": {
                # stored max is only the 60-char prefix, truncated in the file
                "min": prefix + "001_tail_a",
                "max": prefix,
                "max_truncated": True,
                "min_truncated": False,
                "null_count": 0,
            },
            "__stats_version__": "colstats-v1",
        })

    # --- explicit NULL partition --------------------------------------------
    put("__null__", "null_ts.parquet", rows(
        ids=[9001, 9002], tss=[None, None],
        names=["null-1", "null-2"], amounts=[90, None]))
    put("__null__", "no_stats.parquet", rows(
        ids=[9011, 9012], tss=[None, None],
        names=["nostats-1", "nostats-2"], amounts=[91, 92]),
        stats=False)

    return {"root": str(root), "files": written}


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    args = ap.parse_args()
    print(json.dumps(build(Path(args.root)), indent=2))
