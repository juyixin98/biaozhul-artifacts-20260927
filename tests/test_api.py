"""HTTP 接口层测试：响应结构、trace 上界依据、诊断、错误不透出为成功。"""
from __future__ import annotations


def _bulk(client, corpus):
    r = client.post("/entries/bulk", json={"items": corpus})
    assert r.status_code == 200, r.text


def test_complete_empty_prefix_global(client, raw_corpus, log):
    _bulk(client, raw_corpus)
    r = client.get("/complete", params={"k": 3, "trace": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["normalized_prefix"] == ""
    assert [e["id"] for e in body["entries"]] == ["ot-3", "mp-01", "ot-1"]
    assert body["trace"]["location"] == "at_node"
    assert body["trace"]["stats"]["nodes_visited"] >= 1
    log("PASS", "PASS", ids=[e["id"] for e in body["entries"]],
        stats=body["trace"]["stats"])


def test_trace_explains_each_prune_bound(client, raw_corpus, log):
    _bulk(client, raw_corpus)
    body = client.get(
        "/complete", params={"prefix": "multit", "k": 2, "trace": "true"}
    ).json()
    prunes = body["trace"]["prunes"]
    assert prunes, "该查询至少剪掉一个兄弟子树（multithreading）"
    for p in prunes:
        assert p["upper_bound"] < p["best_k_score"]
        assert "可靠上界" in p["justification"] and "严格小于" in p["justification"]
    events = body["trace"]["events"]
    assert any(ev["event"] == "prune" for ev in events)
    assert events[0]["step"] == 0
    log("PASS", "PASS", prunes=prunes)


def test_prefix_normalized_server_side(client, raw_corpus, log):
    _bulk(client, raw_corpus)
    r1 = client.get("/complete", params={"prefix": "MULTI", "k": 3}).json()
    r2 = client.get("/complete", params={"prefix": "multi", "k": 3}).json()
    r3 = client.get("/complete", params={"prefix": "ＭＵＬＴＩ", "k": 3}).json()
    ids = [e["id"] for e in r1["entries"]]
    assert ids == [e["id"] for e in r2["entries"]] == [e["id"] for e in r3["entries"]]
    assert r3["normalized_prefix"] == "multi"
    log("PASS", "PASS", ids=ids)


def test_errors_never_report_success(client, log):
    cases = [
        ("PUT", "/entries/x", {"json": {"id": "x", "surface": "", "score": 1}}, 400, "E_INVALID_INPUT"),
        ("GET", "/complete", {"params": {"k": 0}}, 400, "E_INVALID_INPUT"),
        ("GET", "/complete", {"params": {"k": 99999}}, 400, "E_INVALID_INPUT"),
        ("DELETE", "/entries/missing", {}, 404, "E_ENTRY_NOT_FOUND"),
        ("POST", "/entries/missing/score", {"json": {"score": 3}}, 404, "E_ENTRY_NOT_FOUND"),
        ("POST", "/entries/missing/adjust", {"json": {"delta": 1}}, 404, "E_ENTRY_NOT_FOUND"),
        ("POST", "/snapshots/bad/name/restore", {}, 404, None),  # 路径不匹配
    ]
    for method, path, kw, want_status, want_code in cases:
        resp = getattr(client, method.lower())(path, **kw)
        assert resp.status_code == want_status, (path, resp.status_code, resp.text)
        if want_code is not None:
            body = resp.json()
            assert body["ok"] is False, (path, body)
            assert body["error"]["code"] == want_code, (path, body["error"]["code"])
            assert "message" in body["error"] and body["error"]["message"]
    log("PASS", "PASS", cases=len(cases))


def test_request_id_is_echoed_and_generated(client, raw_corpus, log):
    resp = client.get("/health", headers={"x-request-id": "run-abc-123"})
    assert resp.headers["x-request-id"] == "run-abc-123"
    resp2 = client.get("/health")
    assert resp2.headers["x-request-id"]
    log("PASS", "PASS", echo="run-abc-123", generated=resp2.headers["x-request-id"])


def test_deep_diagnostics_crosschecks_oracle(client, raw_corpus, log):
    _bulk(client, raw_corpus)
    r = client.post("/diagnostics/verify", params={"deep": "true"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    cc = body["cross_check"]
    assert cc["ok"] is True
    assert cc["mismatches"] == []
    assert cc["invalid_bounds"] == []
    assert cc["prefixes_checked"] >= 5
    log("PASS", "PASS", prefixes=cc["prefixes_checked"],
        prune_checks=cc["prune_checks_total"])


def test_bulk_is_atomic_on_invalid_item(client, raw_corpus, log):
    bad = raw_corpus + [{"id": "broken", "surface": "   ", "score": 1}]
    r = client.post("/entries/bulk", json={"items": bad})
    assert r.status_code == 400
    assert client.get("/health").json()["entries"] == 0, "非法批次不得部分写入"
    log("PASS", "PASS", code="E_INVALID_SURFACE/E_INVALID_INPUT", entries_after=0)


def test_bulk_duplicate_id_conflict(client, raw_corpus, log):
    items = [
        {"id": "d1", "surface": "database", "score": 5},
        {"id": "d1", "surface": "different-word", "score": 6},
    ]
    r = client.post("/entries/bulk", json={"items": items})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "E_DUPLICATE_ID"
    log("PASS", "PASS", code="E_DUPLICATE_ID")


def test_scores_are_nonnegative_integers(client, log):
    r = client.put("/entries/x", json={"id": "x", "surface": "abc", "score": 1.5})
    assert r.status_code == 400 and r.json()["ok"] is False
    log("PASS", "PASS", float_score_rejected=True)
