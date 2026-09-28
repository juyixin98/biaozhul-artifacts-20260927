#!/usr/bin/env python3
"""End-to-end service-call example against a locally running server.

Demonstrates the four scenarios the task asks reviewers to verify:

1. concurrent disjoint-partition appends (two threads, same stale base);
2. same-partition overwrite conflict (second writer is rejected, not
   last-writer-wins);
3. lost commit response (same request_id + same payload replays once);
4. orphan folder detection and reconciliation.

Run:
    .venv/bin/uvicorn app.api:app --port 8077 &
    .venv/bin/python examples/run_demo.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

BASE_URL = "http://127.0.0.1:8077"
ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "run_results" / "demo_inputs"
TABLE = "demo_events"


def _write(name: str, rows: list[tuple]) -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    path = WORK / name
    table = pa.table(
        {
            "sale_id": pa.array([r[0] for r in rows], pa.int64()),
            "region": pa.array([r[1] for r in rows], pa.string()),
            "day": pa.array([r[2] for r in rows], pa.string()),
            "amount": pa.array([r[3] for r in rows], pa.int64()),
            "owner": pa.array([r[4] for r in rows], pa.string()),
        }
    )
    pq.write_table(table, path)
    return path


def show(title: str, response: httpx.Response) -> dict:
    print(f"\n=== {title} -> HTTP {response.status_code}")
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = {"raw": response.text}
    print(json.dumps(body, indent=2, ensure_ascii=False)[:2000])
    return body


def main() -> int:
    client = httpx.Client(base_url=BASE_URL, timeout=10)

    # health
    r = client.get("/health")
    if r.status_code != 200:
        print("server is not reachable; start it with:", file=sys.stderr)
        print("  .venv/bin/uvicorn app.api:app --port 8077", file=sys.stderr)
        return 1

    # ---- 0. table -------------------------------------------------------
    client.post("/tables", json={"table": TABLE,
                                 "partition_spec": ["region", "day"]})

    us = _write("us.parquet", [
        (1, "us", "2024-01-01", 10, "alice@example.test"),
        (2, "us", "2024-01-01", 20, "alice@example.test"),
    ])
    eu = _write("eu.parquet", [
        (3, "eu", "2024-01-01", 30, "bob@example.test"),
    ])
    us_v2 = _write("us_v2.parquet", [
        (101, "us", "2024-01-01", 100, "carol@example.test"),
    ])

    def commit(rid: str, path: Path, base: int | None, op: str = "APPEND"):
        return client.post("/commits", json={
            "table": TABLE, "operation": op, "request_id": rid,
            "base_snapshot_id": base, "files": [str(path)],
        })

    # ---- 1. concurrent disjoint appends from the same base (root=1) -----
    barrier = threading.Barrier(2)
    out: dict[str, httpx.Response] = {}

    def t_us():
        barrier.wait()
        out["us"] = commit("req-demo-us", us, 1)

    def t_eu():
        barrier.wait()
        out["eu"] = commit("req-demo-eu", eu, 1)

    threads = [threading.Thread(target=t_us), threading.Thread(target=t_eu)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    show("1a concurrent APPEND us/0101 (base=1)", out["us"])
    show("1b concurrent APPEND eu/0101 (base=1, disjoint -> rebased)",
         out["eu"])

    # ---- 2. same-partition overwrite against stale base ----------------
    head = client.get(f"/tables/{TABLE}/snapshots/latest").json()["snapshot_id"]
    good = commit("req-demo-ovw-ok", us_v2, head, op="OVERWRITE")
    show("2a OVERWRITE us/0101 from current HEAD", good)
    stale = commit("req-demo-ovw-stale", us_v2, 1, op="OVERWRITE")
    show("2b OVERWRITE us/0101 from stale base=1 -> 409 conflict", stale)
    assert stale.status_code == 409
    assert stale.json()["error"] == "CONFLICT_OVERLAPPING_PARTITION"

    # ---- 3. lost response: replay the exact same request ---------------
    replay1 = commit("req-demo-us", us, 1)
    replay2 = commit("req-demo-us", us, 1)
    b1 = show("3a first call (already committed earlier -> stored outcome)",
              replay1)
    b2 = show("3b identical retry (same request_id + same bytes)", replay2)
    assert b1["snapshot_id"] == b2["snapshot_id"]
    assert b2["replayed"] is True

    # ---- 4. orphan folder -----------------------------------------------
    orphan_dir = ROOT / "warehouse" / "_orphan_demo"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"x": [1]}), orphan_dir / "stray.parquet")
    scan = show("4a orphan scan", client.get("/maintenance/orphans"))
    assert "_orphan_demo/stray.parquet" in scan["orphan_files"]
    rec = show("4b orphan reconcile",
               client.post("/maintenance/orphans/reconcile"))
    assert "_orphan_demo/stray.parquet" in rec["removed_files"]

    # ---- final snapshot + commit log ------------------------------------
    final = client.get(f"/tables/{TABLE}/snapshots/latest").json()
    print("\n=== FINAL latest snapshot")
    print(json.dumps(final, indent=2, ensure_ascii=False))
    log = client.get("/commits", params={"table": TABLE}).json()
    print("\n=== FINAL commit log")
    for row in log:
        print(f"  {row['request_id']:22s} {str(row['status']):9s} "
              f"op={str(row['operation']):9s} snap={row['final_snapshot_id']} "
              f"err={row['error_category']}")
    print("\nDemo finished successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
