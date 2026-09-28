#!/usr/bin/env python3
"""End-to-end acceptance script.

Runs:
  1. the pytest suite (all assertions are against independent references);
  2. a live-server smoke test over HTTP (uvicorn + httpx): schema, ingest of
     boundary/negative coordinates, range query, budget-exhausted query,
     compaction with stable row ids, full-scan cross-check, /api/verify;
  3. a larger synthetic scale run reporting candidate inflation and chunk IO.

The report is written to reports/run-<timestamp>.json AND printed.  Results are
recorded truthfully: any failure sets a non-zero exit code and the report
keeps the offending output.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from zcluster.config import Config  # noqa: E402
from zcluster.core.store import Store  # noqa: E402
from zcluster.kernel.coder import DimSpec  # noqa: E402
from zcluster.verification.checks import run_all  # noqa: E402
from zcluster.api.app import temporary_store_factory  # noqa: E402


def run_pytest() -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short"],
        cwd=ROOT, capture_output=True, text=True,
    )
    tail = "\n".join(proc.stdout.strip().splitlines()[-8:])
    return {"returncode": proc.returncode, "summary_tail": tail}


def wait_for_health(base: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.3)
    raise RuntimeError(f"server did not become healthy: {last}")


def run_server_smoke(server_root: str, port: int) -> dict:
    env = dict(os.environ)
    # The server must start from a clean data root for a truthful from-scratch
    # acceptance run; config is materialized into the per-run work directory.
    cfg_dir = Path(server_root) / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "acceptance.json"
    cfg_path.write_text(json.dumps({
        "data_root": os.path.join(server_root, "data"),
        "chunk_size": 200,
        "query": {"default_interval_budget": 256,
                  "max_interval_budget": 1000000},
        "storage": {"arrow_compression": "zstd",
                    "code_uint64_when_fit": True},
        "logging": {"level": "WARNING",
                    "file": os.path.join(server_root, "server.log")},
    }), encoding="utf-8")
    env["ZCLUSTER_CONFIG"] = str(cfg_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "zcluster.api.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    transcript: list[dict] = []
    try:
        wait_for_health(base)
        h = {"X-Request-Id": "acceptance"}
        with httpx.Client(base_url=base, timeout=30) as client:
            r = client.post("/api/schema", headers=h, json={
                "name": "points",
                "dimensions": [
                    {"name": "x", "bits": 6, "signed": True},
                    {"name": "y", "bits": 6, "signed": True},
                ],
            })
            transcript.append({"step": "schema", "status": r.status_code})
            assert r.status_code == 200, r.text

            rng = random.Random(20260928)
            # boundary + negative anchors plus random fill
            rows = [{"x": -32, "y": -32}, {"x": -32, "y": 31},
                    {"x": 31, "y": -32}, {"x": 31, "y": 31},
                    {"x": -1, "y": 0}, {"x": 0, "y": -1}]
            rows += [{"x": rng.randint(-32, 31), "y": rng.randint(-32, 31)}
                     for _ in range(2000)]
            r = client.post("/api/ingest", headers=h, json={"rows": rows})
            assert r.status_code == 200, r.text
            ing = r.json()["ingest"]
            transcript.append({"step": "ingest", "status": 200,
                               "accepted": ing["accepted"],
                               "chunks": len(ing["chunks"])})

            box = {"box": [{"dimension": "x", "lo": -3, "hi": 3},
                           {"dimension": "y", "lo": -3, "hi": 3}]}

            r = client.post("/api/query", headers=h, json=box)
            q = r.json()
            transcript.append({"step": "query_exact", "stats": q["stats"],
                               "uncertainties": q["uncertainties"]})

            r = client.post("/api/full-scan", headers=h, json=box)
            fs = r.json()
            transcript.append({"step": "full_scan", "stats": fs["stats"]})

            q_ids = sorted(row["row_id"] for row in q["rows"])
            fs_ids = sorted(row["row_id"] for row in fs["rows"])
            assert q_ids == fs_ids, "zero-miss check failed over HTTP"
            transcript.append({"step": "zero_miss_crosscheck",
                               "result_rows": len(fs_ids)})

            r = client.post("/api/query", headers=h,
                            json={**box, "interval_budget": 1})
            qb1 = r.json()
            assert qb1["budget_exhausted"] is True
            assert sorted(x["row_id"] for x in qb1["rows"]) == fs_ids
            transcript.append({"step": "query_budget1",
                               "stats": qb1["stats"],
                               "uncertainties": qb1["uncertainties"]})

            r = client.post("/api/compact", headers=h)
            comp = r.json()["compact"]
            transcript.append({"step": "compact", "stats": comp["stats"]})

            r = client.post("/api/full-scan", headers=h, json=box)
            fs2 = r.json()
            assert sorted(x["row_id"] for x in fs2["rows"]) == fs_ids
            transcript.append({"step": "post_compact_zero_miss",
                               "result_rows": len(fs2["rows"])})

            r = client.post("/api/verify", headers=h)
            verify = r.json()["report"]
            transcript.append({"step": "verify", "ok": verify["ok"],
                               "failures": verify["failure_categories"]})
            assert verify["ok"] is True

            r = client.get("/api/requests/acceptance")
            assert r.status_code == 200
            transcript.append({"step": "request_audit_lookup"})

        return {"ok": True, "transcript": transcript}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc), "transcript": transcript}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def run_scale_demo(tmpdir: str) -> dict:
    """Quantify candidate inflation and chunk reads across regimes.

    * ``clustered_3d`` — data grouped into two distant Morton regions, so a box
      over one region shows real chunk pruning and budget gradients;
    * ``sparse_4d`` — uniform 50k rows in 4 dimensions: boxes genuinely
      fragment in Z-order and exhaust moderate budgets; this is the honest
      "budget can inflate but never miss" regime.
    """
    def cfg_for(subdir: str, chunk_size: int) -> Config:
        return Config(
            data_root=os.path.join(tmpdir, subdir), chunk_size=chunk_size,
            default_interval_budget=256, max_interval_budget=2_000_000,
            arrow_compression="zstd", code_uint64_when_fit=True,
            log_level="WARNING", log_file=os.path.join(tmpdir, f"{subdir}.log"),
            source_path=f"acceptance-{subdir}",
        )

    regimes: dict[str, dict] = {}

    # ---- clustered 3D: distant regions -> chunk pruning visible --------
    dims3 = [DimSpec("a", 12, signed=True), DimSpec("b", 12, signed=True),
             DimSpec("c", 8)]
    rng = random.Random(42)
    points3 = (
        [{"a": rng.randint(-2048, -1900), "b": rng.randint(-2048, -1900),
          "c": rng.randrange(20)} for _ in range(20000)]
        + [{"a": rng.randint(1900, 2047), "b": rng.randint(1900, 2047),
            "c": rng.randrange(236, 256)} for _ in range(20000)]
    )
    box3 = [
        {"dimension": "a", "lo": -2048, "hi": -2030},
        {"dimension": "b", "lo": -2048, "hi": -2030},
        {"dimension": "c", "lo": 0, "hi": 10},
    ]
    with Store(cfg_for("cluster3", 2000)) as store:
        store.initialize("cluster3", [d.to_dict() for d in dims3], "s3-init")
        store.ingest(points3, "s3-ingest")
        store.compact("s3-compact")
        fs = store.full_scan(box3, "s3-scan")
        truth = sorted(r["row_id"] for r in fs["rows"])
        per = []
        for budget in (1, 64, 4096):
            o = store.query(box3, f"s3-q{budget}", budget=budget)
            assert sorted(r["row_id"] for r in o.rows) == truth
            per.append(brief_stats(o))
        regimes["clustered_3d"] = {
            "truth_rows": len(truth),
            "full_scan_bytes": fs["stats"]["bytes_read"],
            "budgets": per, "zero_miss": True,
        }

    # ---- sparse uniform 4D: genuine Z fragmentation --------------------
    dims4 = [DimSpec("x", 12, signed=True), DimSpec("y", 12, signed=True),
             DimSpec("z", 8), DimSpec("t", 8)]
    points4 = [{"x": rng.randint(-2048, 2047), "y": rng.randint(-2048, 2047),
                "z": rng.randrange(256), "t": rng.randrange(256)}
               for _ in range(50000)]
    box4 = [
        {"dimension": "x", "lo": -100, "hi": 300},
        {"dimension": "y", "lo": -50, "hi": 200},
        {"dimension": "z", "lo": 10, "hi": 240},
        {"dimension": "t", "lo": 100, "hi": 150},
    ]
    post_compact = None
    with Store(cfg_for("sparse4", 2000)) as store:
        store.initialize("sparse4d", [d.to_dict() for d in dims4], "s4-init")
        store.ingest(points4, "s4-ingest")
        fs = store.full_scan(box4, "s4-scan")
        truth = sorted(r["row_id"] for r in fs["rows"])
        per = []
        for budget in (1, 256, 100_000):
            o = store.query(box4, f"s4-q{budget}", budget=budget)
            assert sorted(r["row_id"] for r in o.rows) == truth
            per.append(brief_stats(o))
        regimes["sparse_4d"] = {
            "truth_rows": len(truth),
            "full_scan_bytes": fs["stats"]["bytes_read"],
            "budgets": per, "zero_miss": True,
        }
        store.compact("s4-compact")
        post_compact = brief_stats(store.query(box4, "s4-after", budget=256))
        assert sorted(r["row_id"]
                      for r in store.full_scan(box4, "s4-scan-after")["rows"]) \
            == truth

    return {"rows": len(points3) + len(points4), "regimes": regimes,
            "post_compaction": post_compact}


def brief_stats(o):
    return {k: o.stats[k] for k in (
        "intervals", "interval_budget", "budget_exhausted",
        "chunks_total", "chunks_read", "chunks_skipped", "bytes_read",
        "candidate_rows", "result_rows", "false_positive_rows",
        "candidate_inflation_ratio")}


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    import tempfile
    workdir = tempfile.mkdtemp(prefix=f"zcluster-acceptance-{ts}-")

    report = {"started_at": ts, "workdir": workdir, "python": sys.version.split()[0]}

    report["pytest"] = run_pytest()
    report["in_process_verify"] = None

    cfg = Config(
        data_root=workdir, chunk_size=4, default_interval_budget=64,
        max_interval_budget=100000, arrow_compression="zstd",
        code_uint64_when_fit=True, log_level="WARNING",
        log_file=os.path.join(workdir, "v.log"), source_path="acceptance",
    )
    report["in_process_verify"] = run_all(cfg, temporary_store_factory(cfg))
    server_root = os.path.join(workdir, "server")
    report["server_smoke"] = run_server_smoke(server_root, port=_free_port())
    report["scale_demo"] = run_scale_demo(workdir)

    ok = (
        report["pytest"]["returncode"] == 0
        and report["in_process_verify"]["ok"]
        and report["server_smoke"]["ok"]
        and all(r["zero_miss"] for r in report["scale_demo"]["regimes"].values())
    )
    report["ok"] = ok
    report["finished_at"] = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    out = reports / f"run-{ts}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nREPORT WRITTEN: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
