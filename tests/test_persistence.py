"""Durability: rebuild hot streaming state from SQLite after a restart.

Simulates a process restart by disposing the in-memory matcher cache while
keeping the same database file, then continues streaming. Node ids are only
ever interpreted with the pinned version's freshly-loaded automaton.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.helpers import b64, create_version, feed, hit_tuples, page_all
from tests.oracle import naive_stream


def test_scan_resumes_after_simulated_restart(tmp_path):
    db = tmp_path / "restart.db"
    settings = Settings(
        db_path=str(db), secret="persist-secret",
        max_patterns=100, max_pattern_bytes=1000,
        max_chunk_bytes=1 << 20, default_page_limit=4, max_page_limit=100,
    )

    app1 = create_app(settings)
    with TestClient(app1) as c1:
        vid = create_version(c1, [b"abcde", b"cde"])
        sid = None
        sid = c1.post("/scans", json={"version_id": vid}).json()["scan_id"]
        # Pattern split across the restart boundary.
        feed(c1, sid, b"xxab")
        # Simulate process death: drop all hot state, keep only SQLite.
        app1.state.container.shutdown()

    app2 = create_app(settings)  # same db path, empty process-local caches
    with TestClient(app2) as c2:
        st = c2.get(f"/scans/{sid}").json()
        assert st["bytes_consumed"] == 4
        assert st["version_id"] == vid
        # Continue the pattern across the "restart".
        feed(c2, sid, b"cdeyy")
        got = hit_tuples(page_all(c2, sid, limit=2))
        want = sorted(naive_stream([b"xxabcdeyy"], [b"abcde", b"cde"]))
        assert got == want
        app2.state.container.shutdown()
