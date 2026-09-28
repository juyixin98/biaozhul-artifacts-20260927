"""End-to-end HTTP behaviour via FastAPI TestClient.

These tests assert specific results and specific failure categories — not
merely that the endpoint responds.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# --------------------------------------------------------------------------
# health & startup
# --------------------------------------------------------------------------

def test_health_reports_no_lexicon_when_empty(client: TestClient):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "no_lexicon"
    assert body["latest_version"] is None
    assert "request_id" in body


def test_seeded_startup_serves_seed_version(seeded_client: TestClient):
    h = client_health(seeded_client)
    assert h["status"] == "ok"
    assert h["latest_version"] == 1
    versions = seeded_client.get("/versions").json()
    assert versions[0]["note"] == "seed:seed_lexicon.json"
    assert versions[0]["word_count"] > 40


def client_health(c: TestClient) -> dict:
    return c.get("/health").json()


# --------------------------------------------------------------------------
# segmentation success with concrete values
# --------------------------------------------------------------------------

def test_segment_ambiguous_clause_returns_optimum_and_gap(seeded_client: TestClient):
    r = seeded_client.post("/segment", json={"text": "研究生命"})
    assert r.status_code == 200
    body = r.json()
    assert [s["surface"] for s in body["segments"]] == ["研究", "生命"]
    assert all(s["type"] == "dict" for s in body["segments"])
    assert body["version_id"] == 1
    assert body["pinned"] is False
    assert body["gap_class"] == "clear"
    assert body["decision"] == "accepted"
    # second best is 研究生|命, strictly worse; gap > 0
    assert body["second_best_cost"] > body["best_cost"]
    assert body["cost_gap"] > 1.0
    # segment costs add up to the reported optimum
    assert sum(s["cost"] for s in body["segments"]) == pytest.approx(body["best_cost"], abs=1e-5)
    cov = body["coverage"]
    assert cov["contiguous"] is True and cov["complete"] is True
    assert cov["raw_start"] == 0 and cov["raw_end"] == 4 and cov["raw_length"] == 4
    # offsets
    assert [(s["raw_start"], s["raw_end"]) for s in body["segments"]] == [(0, 2), (2, 4)]


def test_segment_unknown_chars_never_dropped(seeded_client: TestClient):
    r = seeded_client.post("/segment", json={"text": "星巴克"})
    body = r.json()
    assert r.status_code == 200
    assert len(body["segments"]) == 1
    seg = body["segments"][0]
    assert seg["type"] == "unknown"
    assert seg["surface"] == "星巴克"
    assert seg["norm_end"] - seg["norm_start"] == 3
    assert seg["cost"] == pytest.approx(60.0)  # 3 chars * 20.0
    assert body["coverage"]["complete"] is True
    assert body["gap_class"] == "no_alternative"


def test_segment_repeated_word(seeded_client: TestClient):
    body = seeded_client.post("/segment", json={"text": "哈哈哈哈"}).json()
    assert [s["surface"] for s in body["segments"]] == ["哈哈", "哈哈"]


def test_segment_variable_length_normalization(seeded_client: TestClient):
    # full-width input + ß expansion -> matches the lowercase lexicon word
    body = seeded_client.post("/segment", json={"text": "ｓｔｒａßｅ"}).json()
    assert body["normalized_text"] == "strasse"
    seg = body["segments"][0]
    assert seg["surface"] == "strasse" and seg["type"] == "dict"
    # 6 raw characters (incl. ß), 7 normalized characters
    assert seg["raw_start"] == 0 and seg["raw_end"] == 6
    assert seg["norm_start"] == 0 and seg["norm_end"] == 7
    assert body["char_map"][4] == body["char_map"][5] == 4
    assert body["coverage"]["complete"] is True
    assert "".join(s["raw_text"] for s in body["segments"]) == "ｓｔｒａßｅ"


def test_deleted_characters_are_reported_and_coverage_stays_contiguous(seeded_client: TestClient):
    raw = "研­究生命"
    body = seeded_client.post("/segment", json={"text": raw}).json()
    assert body["deleted_raw_indices"] == [1]
    assert "".join(s["raw_text"] for s in body["segments"]) == raw
    assert body["coverage"]["contiguous"] is True


# --------------------------------------------------------------------------
# request id
# --------------------------------------------------------------------------

def test_request_id_is_generated_and_echoed(seeded_client: TestClient):
    r = seeded_client.post("/segment", json={"text": "研究"})
    rid = r.headers["x-request-id"]
    assert len(rid) == 12
    assert r.json()["request_id"] == rid


def test_caller_supplied_request_id_is_honored(seeded_client: TestClient):
    r = seeded_client.post(
        "/segment", json={"text": "研究"}, headers={"X-Request-ID": "trace-abc-123"}
    )
    assert r.headers["x-request-id"] == "trace-abc-123"
    assert r.json()["request_id"] == "trace-abc-123"


# --------------------------------------------------------------------------
# failure categories
# --------------------------------------------------------------------------

def test_empty_text_is_400_empty_text(client: TestClient):
    r = client.post("/segment", json={"text": ""})
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "EMPTY_TEXT"
    assert body["request_id"] == r.headers["x-request-id"]


def test_missing_field_is_422_invalid_payload(client: TestClient):
    r = client.post("/segment", json={})
    assert r.status_code == 422
    assert r.json()["error"] == "INVALID_PAYLOAD"


def test_non_string_text_is_422(client: TestClient):
    r = client.post("/segment", json={"text": 123})
    assert r.status_code == 422
    assert r.json()["error"] == "INVALID_PAYLOAD"


def test_invalid_pinned_version_zero_is_422(client: TestClient):
    r = client.post("/segment", json={"text": "a", "version_id": 0})
    assert r.status_code == 422


def test_text_too_long_is_413(client: TestClient):
    client_post_version(client, [{"word": "a", "freq": 1}])  # ensure lexicon exists
    r = client.post("/segment", json={"text": "a" * 1001})
    assert r.status_code == 413
    assert r.json()["error"] == "TEXT_TOO_LONG"


def test_pinned_missing_version_is_404(client: TestClient):
    client_post_version(client, [{"word": "a", "freq": 1}])
    r = client.post("/segment", json={"text": "a", "version_id": 42})
    assert r.status_code == 404
    assert r.json()["error"] == "VERSION_NOT_FOUND"


def test_segment_without_any_version_is_409(client: TestClient):
    r = client.post("/segment", json={"text": "研究"})
    assert r.status_code == 409
    assert r.json()["error"] == "EMPTY_LEXICON"


# --------------------------------------------------------------------------
# whole-version publishing and version pinning
# --------------------------------------------------------------------------

def client_post_version(c: TestClient, words, note=""):
    return c.post("/versions", json={"words": words, "note": note})


def test_publish_whole_version_then_segment(client: TestClient):
    r = client_post_version(
        client,
        [
            {"word": "ab", "freq": 1000},
            {"word": "cd", "freq": 1000},
            {"word": "a", "freq": 700},
            {"word": "bcd", "freq": 1000},
        ],
        note="v1 fixture",
    )
    assert r.status_code == 201
    body = r.json()
    assert body["version_id"] == 1
    assert body["word_count"] == 4
    assert len(body["checksum"]) == 16

    seg = client.post("/segment", json={"text": "abcd", "version_id": 1}).json()
    assert [s["surface"] for s in seg["segments"]] == ["ab", "cd"]
    assert seg["pinned"] is True
    assert seg["gap_class"] == "close"
    assert seg["decision"] == "indeterminate"


def test_pinned_request_stays_on_old_version_after_new_publish(client: TestClient):
    # v1: ab|cd wins (close gap)
    client_post_version(client, [
        {"word": "ab", "freq": 1000}, {"word": "cd", "freq": 1000},
        {"word": "a", "freq": 700}, {"word": "bcd", "freq": 1000},
    ])
    # v2: a|bcd becomes the clear winner; full replacement dictionary
    client_post_version(client, [
        {"word": "ab", "freq": 100}, {"word": "cd", "freq": 100},
        {"word": "a", "freq": 5000}, {"word": "bcd", "freq": 5000},
    ])
    assert client.get("/health").json()["latest_version"] == 2

    pinned_v1 = client.post("/segment", json={"text": "abcd", "version_id": 1}).json()
    latest = client.post("/segment", json={"text": "abcd"}).json()

    assert [s["surface"] for s in pinned_v1["segments"]] == ["ab", "cd"]
    assert pinned_v1["version_id"] == 1 and pinned_v1["pinned"] is True
    assert [s["surface"] for s in latest["segments"]] == ["a", "bcd"]
    assert latest["version_id"] == 2 and latest["pinned"] is False

    # publish v3; an already-resolved pin of v1 must still answer identically
    client_post_version(client, [{"word": "xyz", "freq": 1}])
    pinned_again = client.post("/segment", json={"text": "abcd", "version_id": 1}).json()
    assert [s["surface"] for s in pinned_again["segments"]] == ["ab", "cd"]
    assert pinned_again["best_cost"] == pinned_v1["best_cost"]


def test_publish_rejects_empty_word_list(client: TestClient):
    r = client.post("/versions", json={"words": []})
    assert r.status_code == 422


def test_publish_rejects_negative_freq(client: TestClient):
    # schema constraint rejects negative frequency before reaching the store
    r = client_post_version(client, [{"word": "a", "freq": -5}])
    assert r.status_code == 422
    assert r.json()["error"] == "INVALID_PAYLOAD"
    # rejected publish created no version
    assert client.get("/health").json()["latest_version"] is None


def test_publish_rejects_build_time_error(client: TestClient):
    # a syntactically valid payload that fails to build a version (word far
    # over the length cap) is a 400 INVALID_PAYLOAD from the service layer.
    r = client_post_version(client, [{"word": "x" * 65, "freq": 1}])
    assert r.status_code == 400
    assert r.json()["error"] == "INVALID_PAYLOAD"
    assert client.get("/health").json()["latest_version"] is None


def test_versions_listing(client: TestClient):
    client_post_version(client, [{"word": "a", "freq": 1}], note="first")
    client_post_version(client, [{"word": "a", "freq": 2}, {"word": "b", "freq": 3}], note="second")
    rows = client.get("/versions").json()
    assert [r["version_id"] for r in rows] == [1, 2]
    assert [r["word_count"] for r in rows] == [1, 2]
    assert rows[1]["note"] == "second"


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------

def test_diagnostics_record_explains_decision_with_redaction(seeded_client: TestClient):
    secret = "我的密码是1234567890"
    r = seeded_client.post(
        "/segment", json={"text": secret}, headers={"X-Request-ID": "diag-1"}
    )
    assert r.status_code == 200
    rec_resp = seeded_client.get("/diagnostics", params={"request_id": "diag-1"}).json()
    rec = rec_resp["record"]
    assert rec is not None
    assert rec["request_id"] == "diag-1"
    assert rec["outcome"] in ("accepted", "indeterminate")
    assert rec["reason"] == "OK"
    assert rec["version_id"] == 1
    assert rec["input_chars"] == len(secret)
    # sensitive content must not appear in full
    assert secret not in rec["input_preview"]
    assert "密码" not in rec["input_preview"]
    assert "len=%d" % len(secret) in rec["input_preview"]


def test_diagnostics_record_rejection_category(seeded_client: TestClient):
    seeded_client.post(
        "/segment", json={"text": ""}, headers={"X-Request-ID": "diag-err"}
    )
    rec = seeded_client.get(
        "/diagnostics", params={"request_id": "diag-err"}
    ).json()["record"]
    assert rec["outcome"] == "rejected"
    assert rec["reason"] == "EMPTY_TEXT"


def test_diagnostics_unknown_request_returns_null_record(seeded_client: TestClient):
    rec = seeded_client.get(
        "/diagnostics", params={"request_id": "nope"}
    ).json()["record"]
    assert rec is None


def test_diagnostics_recent_lists_decisions(seeded_client: TestClient):
    seeded_client.post("/segment", json={"text": "研究生命"})
    seeded_client.post("/segment", json={"text": ""})
    records = seeded_client.get("/diagnostics", params={"limit": 10}).json()["records"]
    reasons = {r["reason"] for r in records}
    assert "OK" in reasons and "EMPTY_TEXT" in reasons
    # recent first
    assert records[0]["reason"] == "EMPTY_TEXT"
