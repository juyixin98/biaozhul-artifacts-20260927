"""HTTP 接口测试：具体响应断言、错误类别、诊断剪枝依据、请求身份关联。"""

from __future__ import annotations


def bulk(client, entries, **kw):
    return client.post("/api/v1/entries:bulkUpsert", json={"entries": entries, **kw})


SEED = [
    {"id": "h", "term": "prefix_hotword", "score": 100},
    {"id": "m", "term": "prefix_middle", "score": 50},
    {"id": "l", "term": "prefix_low", "score": 10},
    {"id": "cafe1", "term": "ＣＡＦＥ", "score": 7},
    {"id": "cafe2", "term": "cafe", "score": 7},
]


class TestCompletionAPI:
    def test_completion_concrete_results(self, client, run_id, log):
        r = bulk(client, SEED)
        assert r.status_code == 201 or r.status_code == 200
        r = client.get("/api/v1/complete", params={"prefix": "prefix", "k": 2})
        body = r.json()
        log.info("[%s] /complete prefix=prefix -> %s", run_id,
                 [(x["id"], x["score"]) for x in body["results"]])
        assert body["count"] == 2
        assert [x["id"] for x in body["results"]] == ["h", "m"]
        assert body["normalizer_version"] == "norm-v1"
        assert body["version"] >= 2
        # 请求身份回传
        assert r.headers["x-request-id"].startswith(run_id)

    def test_empty_prefix_global_topk(self, client):
        bulk(client, SEED)
        body = client.get("/api/v1/complete", params={"prefix": "", "k": 3}).json()
        assert [x["id"] for x in body["results"]] == ["h", "m", "l"]

    def test_tie_collision_order_over_http(self, client):
        bulk(client, SEED)
        body = client.get(
            "/api/v1/complete", params={"prefix": "CAFE", "k": 5}
        ).json()  # 大写查询也应命中（规范化后等价）
        ids = [x["id"] for x in body["results"]]
        # 同分、同 term_norm(cafe)：按 display 码位 CAFE? 实际原文 "cafe" 与全角
        displays = [(x["term"], x["id"]) for x in body["results"]]
        assert displays == [("cafe", "cafe2"), ("ＣＡＦＥ", "cafe1")], displays
        assert ids == ["cafe2", "cafe1"]

    def test_diagnostics_trace_explains_pruning(self, client, run_id, log):
        bulk(client, SEED)
        # 先降权热词
        client.post("/api/v1/entries:bulkUpsert", json={
            "entries": [{"id": "h", "term": "prefix_hotword", "score": 1}]
        })
        r = client.get(
            "/api/v1/complete",
            params={"prefix": "", "k": 2, "diagnostics": "true"},
        )
        body = r.json()
        diag = body.get("diagnostics")
        assert diag is not None, "失败类别: diagnostics=true 未返回计算轨迹"
        log.info("[%s] 诊断轨迹 pushed=%d expanded=%d pruned=%d decisions=%d",
                 run_id, diag["pushed"], diag["expanded"],
                 diag["pruned_children"], len(diag["decisions"]))
        assert [x["id"] for x in body["results"]] == ["m", "l"]
        # 至少出现一次基于 max_score 上界的整枝/终止判定，且理由文案明确
        reasons = " ".join(d["reason"] for d in diag["decisions"])
        assert "上界" in reasons, "失败类别: 剪枝决策未说明上界依据"
        for d in diag["decisions"]:
            assert d["decision"] in {"prune", "expand"}
            if d["decision"] == "prune" and d["threshold_score"] is not None:
                assert d["subtree_best_score"] <= d["threshold_score"]

    def test_prefix_normalization_returned(self, client):
        bulk(client, [{"id": "x", "term": "Straße", "score": 5}])
        body = client.get("/api/v1/complete", params={"prefix": "STRASSE"}).json()
        assert body["prefix_norm"] == "strasse"
        assert body["results"][0]["term"] == "Straße"  # 展示原文保留
        assert body["results"][0]["term_norm"] == "strasse"


class TestErrorSemantics:
    def test_empty_term_is_validation_error(self, client):
        r = bulk(client, [{"id": "x", "term": "", "score": 1}])
        assert r.status_code == 400
        assert r.json()["error_code"] == "VALIDATION_ERROR"

    def test_extra_field_rejected(self, client):
        r = bulk(client, [{"id": "x", "term": "a", "score": 1, "bogus": 2}])
        assert r.status_code == 400
        assert r.json()["error_code"] == "VALIDATION_ERROR"

    def test_nan_score_rejected(self, client):
        # Python json 默认会把 float('nan') 序列化为 NaN 字面量；
        # 服务端必须把这种非法分值判为校验失败而不是接受。
        r = client.post(
            "/api/v1/entries:bulkUpsert",
            content='{"entries": [{"id": "x", "term": "a", "score": NaN}]}',
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400
        assert r.json()["error_code"] == "VALIDATION_ERROR"

    def test_invalid_k(self, client):
        bulk(client, [{"id": "x", "term": "a", "score": 1}])
        for bad_k in (0, -1, 9999):
            r = client.get("/api/v1/complete", params={"k": bad_k})
            assert r.status_code == 400
            assert r.json()["error_code"] == "VALIDATION_ERROR", bad_k

    def test_delete_missing_returns_404(self, client):
        r = client.post("/api/v1/entries:delete", json={"id": "nope"})
        assert r.status_code == 404
        body = r.json()
        assert body["error_code"] == "ENTRY_NOT_FOUND"
        assert body["request_id"]

    def test_missing_version_404(self, client):
        r = client.get("/api/v1/complete", params={"version": 424242})
        assert r.status_code == 404
        assert r.json()["error_code"] == "VERSION_NOT_FOUND"

    def test_restore_missing_snapshot_404(self, client):
        r = client.post("/api/v1/snapshots:restore", json={"version": 424242})
        assert r.status_code == 404
        assert r.json()["error_code"] == "VERSION_NOT_FOUND"

    def test_malformed_json_not_success(self, client):
        r = client.post(
            "/api/v1/entries:bulkUpsert",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400
        assert r.json()["error_code"] == "VALIDATION_ERROR"


class TestStatusAndVersions:
    def test_status_reports_health_and_structure(self, client):
        bulk(client, [{"id": "a", "term": "international", "score": 3},
                      {"id": "b", "term": "internationalize", "score": 4}])
        body = client.get("/api/v1/status").json()
        assert body["healthy"] is True
        assert body["stored_entries"] == body["indexed_entries"] == 2
        assert body["node_count"] >= 2
        assert body["violations"] == []
        assert body["normalizer_version"] == "norm-v1"

    def test_versions_and_snapshots_endpoints(self, client):
        bulk(client, [{"id": "a", "term": "apple", "score": 1}])
        snap = client.post("/api/v1/snapshots", json={"note": "v-snap"}).json()
        versions = client.get("/api/v1/versions").json()
        kinds = {v["kind"] for v in versions}
        assert {"baseline", "commit", "snapshot"} <= kinds
        snaps = client.get("/api/v1/snapshots").json()["snapshots"]
        assert any(s["version_id"] == snap["version"] for s in snaps)

        restored = client.post(
            "/api/v1/snapshots:restore",
            json={"version": snap["version"]},
        ).json()
        assert restored["entry_count"] == 1
