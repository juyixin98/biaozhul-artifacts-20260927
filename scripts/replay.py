#!/usr/bin/env python3
"""Replay / inspect merge run logs and metadata from a local SQLite database.

Examples
--------
# List recent runs (seq, status, counts, error category)
.venv/bin/python scripts/replay.py --db data/merge-demo.db list

# Show one run's full decision timeline from the JSONL log
.venv/bin/python scripts/replay.py --log logs/merge-runs.jsonl timeline <run_id>

# Rebuild the inputs the planner saw for a run id
.venv/bin/python scripts/replay.py --db data/merge-demo.db snapshot <run_id>

# Verify a run: replay the recorded source against the recorded target with
# the independent oracle and diff against the recorded action set
.venv/bin/python scripts/replay.py --db data/merge-demo.db verify <run_id>
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.oracle import oracle_plan  # noqa: E402


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def list_runs(db_path: str, limit: int) -> None:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT seq, run_id, status, target_table, source_rows, target_rows, "
        "n_update, n_insert, n_delete, n_unprocessed, error_category, error_code "
        "FROM merge_runs ORDER BY seq DESC LIMIT ?",
        (limit,),
    ).fetchall()
    print(f"{'seq':>4}  {'status':<10} {'upd':>4}{'ins':>5}{'del':>5}  run_id  error")
    for r in rows:
        err = f"{r['error_category'] or ''}/{r['error_code'] or ''}".strip("/")
        print(
            f"{r['seq']:>4}  {r['status']:<10} "
            f"{r['n_update']:>4}{r['n_insert']:>5}{r['n_delete']:>5}  "
            f"{r['run_id']}  {err}"
        )


def snapshot(db_path: str, run_id: str) -> None:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT source_json, target_json FROM merge_snapshots WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        print(f"snapshot not found for run {run_id}", file=sys.stderr)
        sys.exit(2)
    print("SOURCE (recorded at plan time):")
    print(json.dumps(json.loads(row["source_json"]), indent=2, ensure_ascii=False))
    print("\nTARGET (pre-operation snapshot):")
    print(json.dumps(json.loads(row["target_json"]), indent=2, ensure_ascii=False))


def timeline(log_path: str, run_id: str | None) -> None:
    if not os.path.exists(log_path):
        print(f"log file not found: {log_path}", file=sys.stderr)
        sys.exit(2)
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            event = json.loads(line)
            if run_id and event.get("run_id") != run_id:
                continue
            seq = event.get("run_seq", "-")
            kind = event.get("event", "?")
            extra = ""
            if kind == "PLAN":
                extra = json.dumps(event["counts"])
            elif kind in ("RUN_REJECT", "RUN_FAIL"):
                extra = f"{event.get('category')}/{event.get('code')} @ {event.get('stage')} :: {event.get('message')}"
            elif kind == "RUN_COMMIT":
                extra = json.dumps(event.get("committed_actions", event.get("counts", {})))
            print(f"seq={seq:<3} {event.get('ts','')}  {kind:<12} {extra}")


def verify(db_path: str, run_id: str) -> int:
    """Cross-check recorded actions with the independent oracle."""
    conn = _connect(db_path)
    run = conn.execute("SELECT * FROM merge_runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        print(f"run not found: {run_id}", file=sys.stderr)
        return 2

    spec = json.loads(run["spec_json"])
    # Stored spec is the engine's normalized MergeSpec shape ("clauses"); the
    # oracle consumes the request shape ("when_clauses"). Translate.
    if "when_clauses" not in spec and "clauses" in spec:
        translated = []
        for c in spec["clauses"]:
            t = c["type"]
            action = {
                "WHEN_MATCHED_THEN_UPDATE": "update",
                "WHEN_MATCHED_THEN_DELETE": "delete",
                "WHEN_NOT_MATCHED_THEN_INSERT": "insert",
            }[t]
            item = {
                "matched": c["matched"],
                "action": action,
                "condition": c["condition"],
            }
            if c["assignments"]:
                item["assignments"] = c["assignments"]
            translated.append(item)
        spec["when_clauses"] = translated
    snap = conn.execute(
        "SELECT source_json, target_json FROM merge_snapshots WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if snap is None:
        print("recorded run has no snapshot (early reject) - nothing to verify")
        return 0

    source = json.loads(snap["source_json"])
    target = json.loads(snap["target_json"])
    # strip bookkeeping 'index'/'rowid' the oracle does not need, but pass
    # __rowid__ so target rowids line up
    src_rows = [{k: v for k, v in r.items() if k != "index"} for r in source]
    tgt_rows = []
    for r in target:
        rowid = r.pop("rowid")
        r["__rowid__"] = rowid
        tgt_rows.append(r)

    expected = oracle_plan(spec, src_rows, tgt_rows)
    recorded = [
        {
            "outcome": r["outcome"],
            "source_index": r["source_index"],
            "target_rowid": r["target_rowid"],
            "key": json.loads(r["key_json"]),
            "new_values": json.loads(r["new_values_json"]),
        }
        for r in conn.execute(
            "SELECT outcome, source_index, target_rowid, key_json, new_values_json "
            "FROM merge_actions WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
    ]

    def norm(actions):
        return sorted(
            ((a["outcome"], tuple(a["key"]),
              a["target_rowid"], json.dumps(a["new_values"], sort_keys=True))
             for a in actions),
            key=lambda x: (str(x[1]), x[0]),
        )

    exp = norm([a for a in expected["actions"] if a["outcome"] != "UNPROCESSED"])
    got = norm(recorded)
    if exp == got:
        print(f"VERIFY OK: {run_id} action set matches independent oracle "
              f"({len(got)} actions)")
        return 0
    print(f"VERIFY MISMATCH for {run_id}")
    only_expected = set(exp) - set(got)
    only_recorded = set(got) - set(exp)
    for a in only_expected:
        print("  expected-only:", a)
    for a in only_recorded:
        print("  recorded-only:", a)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/merge-demo.db")
    ap.add_argument("--log", default="logs/merge-runs.jsonl")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--limit", type=int, default=20)
    p_snap = sub.add_parser("snapshot")
    p_snap.add_argument("run_id")
    p_tl = sub.add_parser("timeline")
    p_tl.add_argument("run_id", nargs="?")
    p_v = sub.add_parser("verify")
    p_v.add_argument("run_id")
    args = ap.parse_args()

    if args.cmd == "list":
        list_runs(args.db, args.limit)
        return 0
    if args.cmd == "snapshot":
        snapshot(args.db, args.run_id)
        return 0
    if args.cmd == "timeline":
        timeline(args.log, args.run_id)
        return 0
    if args.cmd == "verify":
        return verify(args.db, args.run_id)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
