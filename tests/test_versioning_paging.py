"""Version switching, epoch isolation and resumable pagination."""
from __future__ import annotations

import pytest

from app.errors import StaleCursorError, VersionMismatchError
from tests.helpers import (
    b64, create_version, feed, hit_tuples, open_scan, page_all,
)
from tests.oracle import naive_stream


def test_feed_wrong_version_is_rejected(client):
    v1 = create_version(client, [b"abc"])
    v2 = create_version(client, [b"xyz"])
    sid = open_scan(client, v1)
    resp = client.post(f"/scans/{sid}/chunks",
                       json={"chunk": b64(b"abc"), "version_id": v2})
    assert resp.status_code == 409
    body = resp.json()["error"]
    assert body["code"] == "version_mismatch"
    assert body["decision"] == "reject"
    # And nothing was consumed.
    st = client.get(f"/scans/{sid}").json()
    assert st["bytes_consumed"] == 0
    assert st["version_id"] == v1


def test_explicit_reset_switches_version_and_bumps_epoch(client):
    v1 = create_version(client, [b"abc"])
    v2 = create_version(client, [b"xy"])
    sid = open_scan(client, v1)
    feed(client, sid, b"abc")
    assert client.get(f"/scans/{sid}").json()["epoch"] == 1

    resp = client.post(f"/scans/{sid}/reset", json={"version_id": v2})
    assert resp.status_code == 200
    after = resp.json()
    assert after["version_id"] == v2
    assert after["epoch"] == 2
    assert after["state_node"] == 0
    assert after["bytes_consumed"] == 0

    # New automaton: "abc" no longer matches, "xy" does.
    feed(client, sid, b"abcxy")
    hits = page_all(client, sid, limit=2)
    assert hit_tuples(hits) == [(3, 5, 0)]


def test_cursor_from_old_epoch_is_stale_after_reset(client):
    v1 = create_version(client, [b"a"])
    v2 = create_version(client, [b"b"])
    sid = open_scan(client, v1)
    feed(client, sid, b"aaa")
    first = client.get(f"/scans/{sid}/hits?limit=2").json()
    assert first["next_cursor"]
    old_cursor = first["next_cursor"]

    client.post(f"/scans/{sid}/reset", json={"version_id": v2})
    feed(client, sid, b"bb")
    resp = client.get(f"/scans/{sid}/hits?cursor={old_cursor}")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "stale_cursor"


def test_reset_same_version_rewinds_and_bumps_epoch(client):
    v = create_version(client, [b"ab"])
    sid = open_scan(client, v)
    feed(client, sid, b"ab")
    before = page_all(client, sid)
    assert len(before) == 1
    client.post(f"/scans/{sid}/reset", json={"version_id": v})
    st = client.get(f"/scans/{sid}").json()
    assert st["epoch"] == 2
    # Fresh epoch starts with no hits even though rows for epoch 1 remain.
    page = client.get(f"/scans/{sid}/hits").json()
    assert page["epoch"] == 2
    assert page["hits"] == []
    assert page["total_in_epoch"] == 0


def test_pagination_drains_all_hits_in_order(client):
    # Single point with a massive fan-out: at the final byte, every suffix
    # pattern terminates at once. Use nested suffixes + repeated occurrences.
    patterns = [b"abcde", b"bcde", b"cde", b"de", b"e"]
    v = create_version(client, patterns)
    sid = open_scan(client, v)
    text = b"abcde" * 40
    feed(client, sid, text)

    hits = page_all(client, sid, limit=3)  # page limit smaller than fan-out
    got = hit_tuples(hits)
    want = sorted(naive_stream([text], patterns))
    assert got == want
    # seq is contiguous 0..n-1 and order is canonical (end,start,pid).
    seqs = [h["seq"] for h in hits]
    assert seqs == list(range(len(hits)))
    ordered = [(h["end"], h["start"], h["pattern_id"]) for h in hits]
    assert ordered == sorted(ordered)


def test_pagination_limit_bounds(client):
    v = create_version(client, [b"a"])
    sid = open_scan(client, v)
    feed(client, sid, b"a")
    resp = client.get(f"/scans/{sid}/hits?limit=0")
    # FastAPI Query(ge=1) -> 422 before service.
    assert resp.status_code == 422
    resp = client.get(f"/scans/{sid}/hits?limit=100000")
    assert resp.status_code == 422


def test_tampered_cursor_rejected(client):
    v = create_version(client, [b"a"])
    sid = open_scan(client, v)
    feed(client, sid, b"aa")
    resp = client.get(
        f"/scans/{sid}/hits?cursor=YWJj.MALICIOUS_NOT_A_MAC"
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] in (
        "invalid_cursor", "invalid_base64")


def test_cursor_for_other_scan_rejected(client):
    v = create_version(client, [b"a"])
    s1 = open_scan(client, v)
    s2 = open_scan(client, v)
    feed(client, s1, b"aa")
    cursor = client.get(f"/scans/{s1}/hits?limit=1").json()["next_cursor"]
    resp = client.get(f"/scans/{s2}/hits?cursor={cursor}")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_cursor"


def test_closed_scan_rejects_further_chunks(client):
    v = create_version(client, [b"a"])
    sid = open_scan(client, v)
    client.post(f"/scans/{sid}/close")
    resp = client.post(f"/scans/{sid}/chunks", json={"chunk": b64(b"a")})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "scan_closed"


def test_single_point_massive_fanout_is_recoverable_across_requests(client):
    # 200 suffix patterns all ending at the same position: the classic
    # "single point, huge hits" case. Each page is a fresh HTTP request.
    n = 200
    # Build nested suffixes of one long string P: P[-1], P[-2:], ..., P.
    P = bytes((0x61 + (i % 26)) for i in range(n))
    patterns = [P[k:] for k in range(n)]
    v = create_version(client, patterns)
    sid = open_scan(client, v)
    feed(client, sid, b"\x00" + P)

    drained = page_all(client, sid, limit=16)
    # At the final position exactly n nested suffixes end. Plus possibly more
    # for periodic regions — compare to oracle rather than assume n.
    want = sorted(naive_stream([b"\x00" + P], patterns))
    assert hit_tuples(drained) == want
    assert len(drained) >= n
    # next_cursor correctly terminates.
    last = client.get(f"/scans/{sid}/hits").json()
    assert last["total_in_epoch"] == len(drained)
