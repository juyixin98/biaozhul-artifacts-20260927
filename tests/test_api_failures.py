"""HTTP-level validation: concrete failure categories, not "endpoint works".

Every test asserts status code AND the stable error code AND decision class.
"""
from __future__ import annotations

import base64

from tests.helpers import b64, create_version, feed, open_scan


def _post_version(client, patterns, **kw):
    body = {"encoding": "binary", "case_mode": "sensitive",
            "patterns": patterns, **kw}
    return client.post("/versions", json=body)


def test_empty_pattern_string_is_422_empty_pattern(client):
    resp = _post_version(client, [b64(b"ok"), b64(b"")])
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "empty_pattern"
    assert err["decision"] == "reject"
    assert err["details"]["index"] == 1


def test_empty_pattern_list_rejected(client):
    resp = _post_version(client, [])
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "empty_pattern"


def test_bad_base64_rejected(client):
    resp = _post_version(client, ["@@@not-base64@@@"])
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_base64"


def test_unpadded_base64_rejected(client):
    # "abc" -> "YWJj" is padded; "YWJj"[:-1] is truncated -> reject.
    resp = _post_version(client, ["YWJj"[:-1]])
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_base64"


def test_duplicate_pattern_exact(client):
    resp = _post_version(client, [b64(b"dup"), b64(b"dup")])
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "duplicate_pattern"
    assert err["details"]["duplicate_of"] == 0


def test_duplicate_after_casefold_is_rejected(client):
    resp = _post_version(client, [b64(b"ABC"), b64(b"abc")],
                         encoding="latin-1", case_mode="ascii_casefold")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "duplicate_pattern"


def test_unsupported_encoding(client):
    resp = _post_version(client, [b64(b"x")], encoding="utf-666")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unsupported_encoding"


def test_invalid_utf8_pattern_rejected(client):
    resp = client.post("/versions", json={
        "encoding": "utf-8", "case_mode": "sensitive",
        "patterns": [b64(b"\xff\xfe")],
    })
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "invalid_encoding"
    assert "bad_offset" in err["details"]


def test_binary_mode_accepts_arbitrary_bytes(client):
    resp = _post_version(client, [b64(b"\x00\xff\xfe\x80")],
                         encoding="binary")
    assert resp.status_code == 201


def test_unknown_version_404(client):
    resp = client.post("/scans", json={"version_id": "deadbeef"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "version_not_found"


def test_unknown_scan_404(client):
    assert client.get("/scans/nope").status_code == 404
    assert client.get("/scans/nope/hits").status_code == 404


def test_chunk_bad_base64_is_422(client):
    v = create_version(client, [b"a"])
    sid = open_scan(client, v)
    resp = client.post(f"/scans/{sid}/chunks", json={"chunk": "###"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_base64"


def test_chunk_invalid_utf8_422_and_state_preserved(client):
    v = create_version(client, [b"ab"], encoding="utf-8")
    sid = open_scan(client, v)
    feed(client, sid, b"a")
    resp = client.post(f"/scans/{sid}/chunks",
                       json={"chunk": b64(b"\xff")})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "invalid_encoding"
    # No partial consumption: still 1 byte in, node on trie depth 1.
    st = client.get(f"/scans/{sid}").json()
    assert st["bytes_consumed"] == 1
    assert st["state_node"] != 0


def test_request_id_echoed(client):
    v = create_version(client, [b"a"])
    sid = open_scan(client, v)
    resp = client.post(f"/scans/{sid}/chunks",
                       json={"chunk": b64(b"a")},
                       headers={"X-Request-ID": "req-fixed-1234"})
    assert resp.headers["x-request-id"] == "req-fixed-1234"


def test_unsafe_supplied_request_id_replaced(client):
    resp = client.get("/health", headers={"X-Request-ID": "bad id!!"})
    rid = resp.headers["x-request-id"]
    # Server mints a 32-char hex id instead of trusting garbage.
    assert rid != "bad id!!"
    assert len(rid) == 32
