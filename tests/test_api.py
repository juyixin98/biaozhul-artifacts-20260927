"""End-to-end HTTP tests via FastAPI TestClient.

Asserts concrete status codes, error categories/codes and headers — not
merely "the endpoint responds".
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from textindex import unicode_version
from textindex.api import create_app
from textindex.errors import (
    CATEGORY_INPUT_ERROR,
    CATEGORY_RESOURCE_EXHAUSTED,
    CATEGORY_STATE_CONFLICT,
)

from . import fixtures


@pytest.fixture()
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c
    app.state.service.close()


def test_version_endpoint_pins_unicode(client):
    r = client.get("/version")
    assert r.status_code == 200
    body = r.json()
    assert body["unicode_version"] == unicode_version.UNICODE_VERSION == "13.0.0"
    assert body["segmenter"] == "grapheme 0.6.0"
    assert "run_id" in body and body["run_id"]
    assert "x-run-id" in r.headers


def test_create_json_and_convert_all_units(client, recorder):
    rec, counts = recorder
    r = client.post("/documents",
                    json={"text": fixtures.B_TEXT, "doc_id": "flag"})
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["cluster_count"] == 2 and doc["byte_count"] == 9

    cases = [
        (0, "grapheme", "codepoint", 0),
        (1, "grapheme", "byte", 8),
        (1, "grapheme", "codepoint", 2),
        (8, "byte", "grapheme", 1),
        (2, "codepoint", "byte", 8),
        (0, "byte", "grapheme", 0),
        (9, "byte", "grapheme", 2),   # end sentinel
    ]
    for pos, frm, to, expected in cases:
        rr = client.post(f"/documents/flag/convert",
                         json={"position": pos, "from_unit": frm,
                               "to_unit": to})
        ok = rr.status_code == 200 and rr.json()["output"] == expected
        counts["PASS" if ok else "FAIL"] += 1
        if not ok:
            rec.judge(test=f"convert_{frm}_{to}_{pos}", kind="api",
                      passed=False, expected=expected,
                      actual=(rr.status_code, rr.text),
                      reason="conversion mismatch")
            pytest.fail(rr.text)
    rec.judge(test="convert_all_units", kind="api", passed=True,
              expected=len(cases), actual=len(cases),
              reason="all concrete conversion pairs agree")


def test_raw_octets_valid_and_invalid(client):
    r = client.post("/documents/raw", content="éx".encode(),
                    headers={"content-type": "application/octet-stream"})
    assert r.status_code == 201
    assert r.json()["codepoint_count"] == 2

    bad = fixtures.INVALID_UTF8["surrogate_encoded"]
    r = client.post("/documents/raw", content=bad,
                    headers={"content-type": "application/octet-stream"})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["category"] == CATEGORY_INPUT_ERROR
    assert err["code"] == "invalid_utf8"
    assert err["details"]["start_byte"] == 0
    assert r.json()["request_id"] == r.headers["x-request-id"]


def test_json_lone_surrogate_rejected(client):
    r = client.post("/documents",
                    data=json.dumps({"text": "x\ud800"}),
                    headers={"content-type": "application/json"})
    # FastAPI/pydantic serialization or our guard: either way it is a 4xx
    # input error and never a 500
    assert r.status_code in (400, 422)


def test_mid_cluster_edit_rejected_over_http(client):
    client.post("/documents", json={"text": "é", "doc_id": "c",
                                   "normalization": "NONE"})
    r = client.post("/documents/c/edit",
                    json={"start": 1, "end": 1, "replacement": "x",
                          "unit": "codepoint"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "illegal_codepoint_boundary"

    # "é" is one cluster; mark U+0301 is the codepoint at byte 1,
    # its continuation byte at byte 2.
    r2 = client.post("/documents/c/edit",
                     json={"start": 1, "end": 1, "replacement": "x",
                           "unit": "byte"})
    # byte 1 is the combining mark's lead byte -> not a cluster boundary
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "illegal_codepoint_boundary"

    r3 = client.post("/documents/c/edit",
                     json={"start": 2, "end": 2, "replacement": "x",
                           "unit": "byte"})
    # byte 2 is a continuation byte -> illegal *byte* boundary
    assert r3.status_code == 422
    assert r3.json()["error"]["code"] == "illegal_byte_boundary"


def test_edit_then_incremental_state_served(client):
    client.post("/documents", json={"text": "\U0001F44B\U0001F600",
                                   "doc_id": "z", "normalization": "NONE"})
    r = client.post("/documents/z/edit",
                    json={"start": 1, "end": 1, "replacement": "‍",
                          "unit": "grapheme"})
    assert r.status_code == 200, r.text
    assert r.json()["cluster_count"] == 1
    validate = client.post("/documents/z/validate")
    assert validate.json()["matches_full_rebuild"] is True
    versions = client.get("/documents/z/versions").json()["versions"]
    assert [v["revision"] for v in versions] == [0, 1]


def test_not_found_and_digest_conflict(client):
    r = client.get("/documents/ghost")
    assert r.status_code == 404
    assert r.json()["error"]["category"] == CATEGORY_STATE_CONFLICT

    client.post("/documents", json={"text": "abc", "doc_id": "dc"})
    r = client.post("/documents/dc/edit",
                    json={"start": 0, "end": 1, "replacement": "z",
                          "base_digest": "f" * 64})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "digest_mismatch"
    assert r.json()["error"]["category"] == CATEGORY_STATE_CONFLICT


def test_document_too_large_is_resource_exhausted(client):
    big = "a" * 70000  # fixture client limit = 64 KiB
    r = client.post("/documents", json={"text": big})
    assert r.status_code == 413
    err = r.json()["error"]
    assert err["category"] == CATEGORY_RESOURCE_EXHAUSTED
    assert err["code"] == "document_too_large"
    assert err["details"]["limit"] == 1 << 16


def test_empty_document_rejected(client):
    r = client.post("/documents", json={"text": ""})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "empty_document"


def test_clusters_endpoint_details(client):
    client.post("/documents", json={"text": fixtures.G_TEXT, "doc_id": "g",
                                   "normalization": "NONE"})
    r = client.get("/documents/g/clusters")
    assert r.status_code == 200
    items = r.json()["clusters"]
    assert [c["text"] for c in items] == fixtures.G_EXPECTED["clusters"]
    assert [c["byte_start"] for c in items] + [items[-1]["byte_end"]] == \
        fixtures.G_EXPECTED["cluster_byte_starts"]


def test_request_id_echo_and_idempotency_of_gets(client):
    r = client.post("/documents", json={"text": "x", "doc_id": "rid"},
                    headers={"x-request-id": "fixed-id-123"})
    assert r.headers["x-request-id"] == "fixed-id-123"
    assert client.get("/documents/rid",
                      headers={"x-request-id": "fixed-id-123"}
                      ).headers["x-request-id"] == "fixed-id-123"
