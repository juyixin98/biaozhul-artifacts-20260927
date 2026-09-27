#!/usr/bin/env python3
"""End-to-end smoke check for the AC streaming backend.

Two modes:

* in-process (default): builds the app with a temp SQLite DB and drives the
  full flow through the real ASGI stack with TestClient;
* live (``--base-url http://127.0.0.1:8000``): hits a running uvicorn server.

The script asserts concrete outcomes and compares the service's paginated hit
multiset with the independent naive oracle (tests.oracle). Exits non-zero on
any mismatch, so it doubles as the "expected judgment" in verification docs.
"""
from __future__ import annotations

import argparse
import base64
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.oracle import naive_stream  # noqa: E402


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def drain_hits(http, scan_id: str, limit: int = 10):
    out, cursor = [], None
    while True:
        url = f"/scans/{scan_id}/hits?limit={limit}"
        if cursor:
            url += f"&cursor={cursor}"
        r = http.get(url)
        assert r.status_code == 200, r.text
        body = r.json()
        out.extend(body["hits"])
        cursor = body["next_cursor"]
        if not cursor:
            return out, body["total_in_epoch"]


def run(base_url: str | None) -> int:
    if base_url:
        import httpx
        context = httpx.Client(base_url=base_url)
    else:
        from contextlib import contextmanager
        from fastapi.testclient import TestClient
        from app.config import Settings
        from app.main import create_app
        db = tempfile.mktemp(prefix="ac-smoke-", suffix=".db")
        app = create_app(Settings(
            db_path=db, secret="smoke-secret",
            max_patterns=5000, max_pattern_bytes=65536,
            max_chunk_bytes=1 << 20, default_page_limit=10,
            max_page_limit=1000,
        ))

        @contextmanager
        def _cm():
            with TestClient(app) as c:
                yield c

        context = _cm()

    with context as http:
        # 1) Version with prefix/suffix nesting + binary pattern.
        patterns = [b"abc", b"bc", b"c", b"\x00\xff"]
        r = http.post("/versions", json={
            "name": "smoke",
            "encoding": "binary",
            "case_mode": "sensitive",
            "patterns": [b64(p) for p in patterns],
        })
        assert r.status_code == 201, r.text
        vid = r.json()["version_id"]
        print(f"[1] created version {vid} "
              f"({r.json()['pattern_count']} patterns, "
              f"{r.json()['node_count']} nodes)")

        # 2) Empty pattern is rejected, concretely.
        r = http.post("/versions", json={
            "encoding": "binary", "case_mode": "sensitive",
            "patterns": [b64(b"x"), b64(b"")],
        })
        assert r.status_code == 422 and \
            r.json()["error"]["code"] == "empty_pattern", r.text
        print("[2] empty pattern rejected: 422 empty_pattern")

        # 3) Open scan, stream in awkward chunks including a binary split.
        stream = b"xxabcyy\x00\xffzzcbc"
        cut = [2, 1, 3, 4, 1, 2, len(stream) - 13]  # last gets remainder
        chunks, i = [], 0
        for s in cut:
            chunks.append(stream[i:i+s]); i += s
        if i < len(stream):
            chunks.append(stream[i:])

        r = http.post("/scans", json={"version_id": vid})
        assert r.status_code == 201, r.text
        sid = r.json()["scan_id"]
        for ch in chunks:
            rr = http.post(f"/scans/{sid}/chunks", json={"chunk": b64(ch)})
            assert rr.status_code == 200, rr.text
        print(f"[3] streamed {len(stream)} bytes in {len(chunks)} chunks; "
              f"node={rr.json()['state_node']}")

        # 4) Drain via resumable pagination and compare against oracle.
        hits, total = drain_hits(http, sid, limit=3)
        got = sorted((h["start"], h["end"], h["pattern_id"]) for h in hits)
        want = sorted(naive_stream([stream], patterns))
        assert got == want, f"multiset mismatch:\n got={got}\nwant={want}"
        assert total == len(want)
        print(f"[4] paginated {total} hits across resumable requests; "
              f"multiset equals naive oracle")

        # 5) Version guard + explicit boundary reset.
        r2 = http.post("/versions", json={
            "encoding": "binary", "case_mode": "sensitive",
            "patterns": [b64(b"zz")],
        })
        vid2 = r2.json()["version_id"]
        r = http.post(f"/scans/{sid}/chunks",
                      json={"chunk": b64(b"zz"), "version_id": vid2})
        assert r.status_code == 409 and \
            r.json()["error"]["code"] == "version_mismatch", r.text
        print("[5] cross-version chunk rejected: 409 version_mismatch")

        old_cursor = http.get(f"/scans/{sid}/hits?limit=1").json()["next_cursor"]
        r = http.post(f"/scans/{sid}/reset", json={"version_id": vid2})
        assert r.status_code == 200 and r.json()["epoch"] == 2, r.text
        if old_cursor:
            r = http.get(f"/scans/{sid}/hits?cursor={old_cursor}")
            assert r.status_code == 409 and \
                r.json()["error"]["code"] == "stale_cursor", r.text
        print("[6] explicit reset bumped epoch; old cursor -> stale_cursor")

        # 6) Diagnostics: request id present, and the (sensitive) chunk
        # content "zz" never appears in stored events.
        rid = "smoke-req-0001"
        r = http.post(f"/scans/{sid}/chunks", json={"chunk": b64(b"zz")},
                      headers={"X-Request-ID": rid})
        assert r.status_code == 200
        ev = http.get(f"/diagnostics/requests/{rid}").json()
        assert any(e["kind"] == "decision" for e in ev)
        import json as _json
        assert "zz" not in _json.dumps(ev), "raw chunk content leaked!"
        print("[7] diagnostics carry request id + decision; raw bytes absent")

    print("\nSMOKE OK — all concrete assertions held.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=None,
                    help="Run against a live server, e.g. "
                         "http://127.0.0.1:8000")
    args = ap.parse_args()
    try:
        return run(args.base_url)
    except AssertionError as exc:
        print(f"SMOKE FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
