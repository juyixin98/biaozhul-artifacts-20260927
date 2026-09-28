#!/usr/bin/env python3
"""Local, self-contained demonstration of the cache-key / Vary auditor.

It exercises the real FastAPI application through an in-process test
client against a throwaway SQLite database in var/demo/:

1. Runs a deliberately weak policy (key = path + query) against four
   synthetic evidence fixtures and prints the concrete collision
   request-pair witnesses: language negotiation, compression encoding,
   cross-identity responses, and missing Vary.
2. Fixes the key by adding the negotiation and identity dimensions and
   shows every collision disappears.
3. Shows that Vary: * is a distinct finding that no key addition can
   repair, and that a genuinely missing Vary header is still reported
   after the key fix (the auditor does not invent coverage).
4. Prints replayable run ids, an event-log excerpt and Ed25519 signature
   verification, plus one example of each error category.

Run:  python scripts/demo.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.api import create_app

ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "var" / "demo"
FIXTURES = ROOT / "fixtures"

BROKEN_POLICY = {
    "name": "path-query-only (BROKEN)",
    "covered_dimensions": ["path", "query"],
    "identity": {"mode": "auto"},
    "shared": True,
}
BROKEN_SHARED_IDENTITY_POLICY = {
    "name": "path-query-only, shared identity (BROKEN)",
    "covered_dimensions": ["path", "query"],
    "identity": {"mode": "shared"},
    "shared": True,
}
FIXED_POLICY = {
    "name": "full negotiation + identity dimensions (FIXED)",
    "covered_dimensions": [
        "path", "query", "accept-language", "accept-encoding",
        "accept", "authorization", "cookie",
    ],
    "identity": {"mode": "per_identity"},
    "shared": True,
}

# fixture -> (policy used to create the counterexample)
COUNTEREXAMPLES = [
    ("language", BROKEN_POLICY),
    ("encoding", BROKEN_POLICY),
    ("identity", BROKEN_SHARED_IDENTITY_POLICY),
    ("missing_vary", BROKEN_POLICY),
]


def hr(title: str) -> None:
    print(f"\n{'=' * 76}\n{title}\n{'=' * 76}")


def show_collisions(report: dict) -> bool:
    collisions = [f for f in report["findings"] if f["kind"] == "collision"]
    kinds = {}
    for f in report["findings"]:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    if not collisions:
        print(f"  run {report['run_id']}: no collisions. findings={kinds}")
        return False
    for c in collisions:
        w = c["witness"]
        print(
            f"  run {report['run_id']}: {c['severity'].upper()} collision "
            f"{w['request_a']!r} <-> {w['request_b']!r} "
            f"key_prefix={w['key_prefix']} "
            f"uncovered={w['differing_dimensions']}"
        )
    print(f"    all findings: {kinds}")
    return True


def main() -> int:
    shutil.rmtree(DEMO_DIR, ignore_errors=True)
    (DEMO_DIR / "keys").mkdir(parents=True, exist_ok=True)
    app = create_app(db_path=DEMO_DIR / "audit.db", fixtures_dir=FIXTURES,
                     key_dir=DEMO_DIR / "keys")
    client = TestClient(app)

    failures = 0

    hr("STEP 1  Counterexamples: different requests, same key, different responses")
    broken_runs = {}
    for fixture, policy in COUNTEREXAMPLES:
        print(f"\n-- fixture: {fixture}.json, policy: {policy['name']}")
        r = client.post("/audits", json={"policy": policy, "fixture": fixture})
        assert r.status_code == 201, r.text
        report = r.json()
        broken_runs[fixture] = report["run_id"]
        if not show_collisions(report):
            print("    !! expected a collision counterexample")
            failures += 1

    hr("STEP 2  Repair the key: add negotiation + identity dimensions")
    fixed_runs = {}
    for fixture, _ in COUNTEREXAMPLES:
        r = client.post("/audits", json={"policy": FIXED_POLICY, "fixture": fixture})
        assert r.status_code == 201, r.text
        report = r.json()
        fixed_runs[fixture] = report["run_id"]
        collisions = [f for f in report["findings"] if f["kind"] == "collision"]
        print(f"  {fixture:14s} run {report['run_id']}: collisions={len(collisions)} "
              f"findings={sorted({f['kind'] for f in report['findings']})}")
        if collisions:
            print("    !! collision survived the key fix")
            failures += 1

    hr("STEP 3  Wildcard Vary vs missing Vary (different failure kinds)")
    for fixture, policy in (
        ("vary_wildcard", FIXED_POLICY),
        ("missing_vary", FIXED_POLICY),
    ):
        r = client.post("/audits", json={"policy": policy, "fixture": fixture})
        report = r.json()
        kinds = sorted({f["kind"] for f in report["findings"]})
        print(f"  {fixture:14s} run {report['run_id']}: {kinds}")
        for f in report["findings"]:
            print(f"    - {f['kind']}: {f['rationale']}")
    if report["findings"]:
        print("  Note: the key fix removed collisions but cannot invent the "
              "absent Vary header.")

    hr("STEP 4  Replay log (key intermediate states + rationale)")
    run_id = broken_runs["language"]
    events = client.get(f"/audits/{run_id}/events").json()["events"]
    for e in events:
        d = e["detail"]
        if e["event"] in ("run_started", "request_keyed", "group_formed",
                          "pair_compared", "finding_emitted", "run_finished"):
            print(f"  [{e['seq']:02d}] {e['event']:18s} {json.dumps(d, ensure_ascii=False)}")

    hr("STEP 5  Report signature verification")
    v = client.get(f"/audits/{run_id}/verify").json()
    print(f"  {v}")
    if not v["valid"]:
        failures += 1

    hr("STEP 6  Distinguishable error categories")
    bad_input = client.post("/policies/validate", json={"policy": {"name": "p"}})
    print(f"  input       -> HTTP {bad_input.status_code} "
          f"{bad_input.json()['error']['category']}/{bad_input.json()['error']['code']}")

    once = {"policy": {**BROKEN_POLICY, "run_id": "dup-demo"},
            "fixture": "language", "run_id": "dup-demo"}
    client.post("/audits", json=once)
    conflict = client.post("/audits", json=once)
    print(f"  state       -> HTTP {conflict.status_code} "
          f"{conflict.json()['error']['category']}/{conflict.json()['error']['code']}")

    exhausted = client.post("/audits", json={
        "policy": {**BROKEN_POLICY, "limits": {"max_requests": 1}},
        "fixture": "language",
    })
    print(f"  resource    -> HTTP {exhausted.status_code} "
          f"{exhausted.json()['error']['category']}/{exhausted.json()['error']['code']}")

    cannot_compute = client.post("/audits", json={
        "policy": {"name": "p", "covered_dimensions": ["path", "accept-language"]},
        "evidence": {
            "requests": [{"id": "r1", "method": "GET", "path": "/x",
                          "headers": {"Accept-Language": ["en", "de"]}}],
            "responses": [{"request_id": "r1", "status": 200, "body": "x"}],
        },
    })
    print(f"  computation -> HTTP {cannot_compute.status_code} "
          f"{cannot_compute.json()['error']['category']}/"
          f"{cannot_compute.json()['error']['code']}")

    expected = {
        (bad_input.status_code, "input"),
        (conflict.status_code, "state"),
        (exhausted.status_code, "resource"),
        (cannot_compute.status_code, "computation"),
    }
    if expected != {(400, "input"), (409, "state"), (429, "resource"),
                    (500, "computation")}:
        failures += 1

    hr("RESULT")
    print(f"  broken-policy runs:     {broken_runs}")
    print(f"  fixed-policy runs:      {fixed_runs}")
    if failures == 0:
        print("  DEMO OK: all collision counterexamples reproduced and removed "
              "by the key fix.")
        return 0
    print(f"  DEMO FAILED with {failures} expectation violation(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
