"""End-to-end HTTP tests asserting concrete results and failure classes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "api.db"))
    with TestClient(app) as c:
        yield c


def _put_source(client, sid, text, **kw):
    body = {"text": text, **kw}
    r = client.put(f"/sources/{sid}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _put_ruleset(client, rid, rules):
    r = client.put(f"/rulesets/{rid}", json={"rules": rules})
    assert r.status_code == 200, r.text
    return r.json()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["engine"] == "re2"
    assert "max_source_bytes" in body["limits"]


def test_full_flow_overlap_and_concrete_output(client, run_logger, request):
    _put_source(client, "doc", "cat catalog catbird")
    _put_ruleset(client, "rs", [
        {"rule_id": "cat", "pattern": r"cat", "template": "FELIX", "priority": 5},
        {"rule_id": "catalog", "pattern": r"catalog", "template": "LOG", "priority": 1},
        {"rule_id": "catbird", "pattern": r"catbird", "template": "BIRD", "priority": 1},
    ])
    r = client.post("/sources/doc/plans/rs")
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["edit_count"] == 3
    assert plan["candidates_dropped"] >= 2

    detail = client.get(f"/plans/{plan['plan_id']}").json()
    assert [e["replacement"] for e in detail["edits"]] == ["FELIX", "FELIX", "FELIX"]

    ar = client.post(f"/plans/{plan['plan_id']}/apply", json={}).json()
    assert ar["output"] == "FELIX FELIXalog FELIXbird"
    assert ar["chunks_emitted"] >= 1
    run_logger.check(
        request.node.nodeid,
        "e2e overlap output",
        expected="FELIX FELIXalog FELIXbird",
        actual=ar["output"],
        passed=ar["output"] == "FELIX FELIXalog FELIXbird",
        reason="cat wins all three leftmost spans; catalog/catbird lose overlap "
               "and their untouched tails ('alog'/'bird') remain literal",
        intermediate={"plan": plan["plan_id"], "edits": detail["edits"]},
    )


def test_apply_with_save_creates_new_source_version(client):
    _put_source(client, "doc", "aaa")
    _put_ruleset(client, "rs", [{"rule_id": "a", "pattern": "a", "template": "AA"}])
    pid = client.post("/sources/doc/plans/rs").json()["plan_id"]
    ar = client.post(f"/plans/{pid}/apply", json={"save_result_as": "doc2"}).json()
    assert ar["result_length"] == 6
    got = client.get("/sources/doc2")
    assert got.status_code == 200
    assert got.json()["sha256"] == ar["result_sha256"]


def test_stale_source_version_is_rejected_409(client, run_logger, request):
    _put_source(client, "doc", "abc")
    _put_ruleset(client, "rs", [{"rule_id": "a", "pattern": "a", "template": "X"}])
    pid = client.post("/sources/doc/plans/rs").json()["plan_id"]

    # mutate source after plan creation -> current digest no longer bound
    _put_source(client, "doc", "abd")
    r = client.post(f"/plans/{pid}/apply", json={"source_id": "doc"})
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "source_version_mismatch"
    assert body["category"] == "STATE_CONFLICT"
    run_logger.check(
        request.node.nodeid,
        "stale version rejected",
        expected="source_version_mismatch/409",
        actual=f"{body['error']}/{r.status_code}",
        passed=r.status_code == 409,
        reason="plan digest binds it to 'abc'; current source is 'abd'",
        intermediate=body.get("details"),
    )


def test_explicit_expected_digest_mismatch_is_409(client):
    _put_source(client, "doc", "abc")
    _put_ruleset(client, "rs", [{"rule_id": "a", "pattern": "a", "template": "X"}])
    pid = client.post("/sources/doc/plans/rs").json()["plan_id"]
    r = client.post(
        f"/plans/{pid}/apply",
        json={"source_id": "doc", "expected_sha256": "0" * 64},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "source_version_mismatch"


def test_double_apply_is_409_already_applied(client):
    _put_source(client, "doc", "abc")
    _put_ruleset(client, "rs", [{"rule_id": "a", "pattern": "a", "template": "X"}])
    pid = client.post("/sources/doc/plans/rs").json()["plan_id"]
    assert client.post(f"/plans/{pid}/apply", json={}).status_code == 200
    r = client.post(f"/plans/{pid}/apply", json={})
    assert r.status_code == 409
    assert r.json()["error"] == "already_applied"


def test_invalid_utf8_is_400_text_not_utf8():
    import pytest as _pt

    from app.api.service import Service, ServiceConfig
    from app.errors import TextDecodeError
    from app.storage import Database, Repository

    db = Database(":memory:")
    svc = Service(Repository(db), ServiceConfig())
    with _pt.raises(TextDecodeError) as exc:
        svc.upload_source("x", b"a\xffb")
    assert exc.value.code == "text_not_utf8"
    db.close()


def test_empty_text_is_400(client):
    r = client.put("/sources/e", json={"text": ""})
    assert r.status_code == 400
    assert r.json()["error"] == "empty_text"


def test_bad_pattern_is_400_with_code(client):
    _put_source(client, "doc", "abc")
    r = client.put("/rulesets/bad", json={
        "rules": [{"rule_id": "x", "pattern": "(", "template": "z"}]
    })
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_pattern"


def test_lookaround_is_400_unsupported_syntax(client):
    r = client.put("/rulesets/bad", json={
        "rules": [{"rule_id": "x", "pattern": r"(?=abc)", "template": "z"}]
    })
    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_syntax"


def test_unknown_capture_template_is_400(client):
    r = client.put("/rulesets/bad", json={
        "rules": [{"rule_id": "x", "pattern": r"(a)", "template": "${missing}"}]
    })
    assert r.status_code == 400
    assert r.json()["error"] == "unknown_capture"


def test_capture_missing_during_plan_is_422(client):
    _put_source(client, "doc", "Mr Smith; Ms")
    _put_ruleset(client, "rs", [{
        "rule_id": "t",
        "pattern": r"(Mr|Ms)(?:\s+([A-Z][a-z]+))?",
        "template": "[${1}:$2]",
    }])
    r = client.post("/sources/doc/plans/rs")
    assert r.status_code == 422
    assert r.json()["error"] == "capture_missing"
    assert r.json()["category"] == "COMPUTATION"


def test_missing_entities_are_404(client):
    assert client.get("/sources/nope").status_code == 404
    assert client.get("/plans/nope").status_code == 404
    r = client.post("/sources/nope/plans/x")
    assert r.status_code == 404


def test_multibyte_roundtrip_and_byte_ranges(client):
    _put_source(client, "doc", "a€b 世界")
    _put_ruleset(client, "rs", [
        {"rule_id": "euro", "pattern": r"€", "template": "EURO"},
        {"rule_id": "cjk", "pattern": r"[世界]", "template": "?"},
    ])
    pid = client.post("/sources/doc/plans/rs").json()["plan_id"]
    detail = client.get(f"/plans/{pid}").json()
    # layout bytes: a=0 €=1..4 b=4 sp=5 世=6..9 界=9..12
    spans = [(e["start"], e["end"]) for e in detail["edits"]]
    assert spans == [(1, 4), (6, 9), (9, 12)]
    out = client.post(f"/plans/{pid}/apply", json={}).json()["output"]
    assert out == "aEUROb ??"
