"""API 与诊断测试:具体响应、失败类别、请求标识与脱敏。"""

import json

from tests.conftest import load_fixture


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_merge_clean_via_api(client):
    fx = load_fixture("disjoint-edits")
    resp = client.post(
        "/merge",
        json={"base": fx["base"], "local": fx["local"], "remote": fx["remote"]},
        headers={"X-Request-ID": "req-clean-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "clean"
    assert body["text"] == fx["expected"]["text"]
    assert body["conflicts"] == []
    assert body["request_id"] == "req-clean-1"
    assert body["merge_id"]


def test_merge_conflicted_carries_three_way_ranges(client):
    fx = load_fixture("delete-vs-modify")
    resp = client.post(
        "/merge",
        json={"base": fx["base"], "local": fx["local"], "remote": fx["remote"]},
    )
    body = resp.json()
    assert body["status"] == "conflicted"
    conflict = body["conflicts"][0]
    assert conflict["kind"] == "delete-vs-modify"
    assert conflict["base_range"] == [1, 3]
    assert conflict["local_range"] == [1, 1]
    assert conflict["remote_range"] == [1, 3]
    assert conflict["base_lines"] == ["line2\n", "line3\n"]
    assert conflict["local_lines"] == []
    assert conflict["remote_lines"] == ["line2 modified\n", "line3\n"]


def test_resolve_roundtrip_via_api(client):
    fx = load_fixture("same-point-insert")
    merge = client.post(
        "/merge",
        json={"base": fx["base"], "local": fx["local"], "remote": fx["remote"]},
    ).json()
    merge_id = merge["merge_id"]

    # 缺少显式选择:失败类别 unresolved-conflicts
    resp = client.post(f"/merges/{merge_id}/resolve", json={"choices": {}})
    assert resp.status_code == 422
    assert resp.json()["error"]["category"] == "unresolved-conflicts"

    # 非法选择:失败类别 unknown-choice
    resp = client.post(
        f"/merges/{merge_id}/resolve", json={"choices": {"0": "sideways"}}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["category"] == "unknown-choice"

    # 显式选择 remote:按夹具手写答案重建
    resp = client.post(
        f"/merges/{merge_id}/resolve", json={"choices": {"0": "remote"}}
    )
    assert resp.status_code == 200
    assert resp.json()["resolved_text"] == fx["resolutions"]["remote"]

    # 解决结果应可从记录中查询
    record = client.get(f"/merges/{merge_id}").json()
    assert record["resolved_text"] == fx["resolutions"]["remote"]


def test_resolve_clean_merge_is_a_typed_error(client):
    fx = load_fixture("disjoint-edits")
    merge = client.post(
        "/merge",
        json={"base": fx["base"], "local": fx["local"], "remote": fx["remote"]},
    ).json()
    resp = client.post(f"/merges/{merge['merge_id']}/resolve", json={"choices": {}})
    assert resp.status_code == 409
    assert resp.json()["error"]["category"] == "nothing-to-resolve"


def test_unknown_merge_is_a_typed_error(client):
    resp = client.get("/merges/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["category"] == "merge-not-found"
    resp = client.post("/merges/does-not-exist/resolve", json={"choices": {}})
    assert resp.status_code == 404
    assert resp.json()["error"]["category"] == "merge-not-found"


def test_merge_by_stored_versions(client):
    fx = load_fixture("disjoint-edits")
    ids = {}
    for role in ("base", "local", "remote"):
        resp = client.post(
            "/documents/doc-1/versions", json={"role": role, "content": fx[role]}
        )
        assert resp.status_code == 200
        ids[role] = resp.json()["version_id"]
    listing = client.get("/documents/doc-1/versions").json()
    assert len(listing["versions"]) == 3
    assert "content" not in listing["versions"][0]  # 列表不回显内容

    resp = client.post(
        "/documents/doc-1/merge",
        json={
            "base_version": ids["base"],
            "local_version": ids["local"],
            "remote_version": ids["remote"],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["text"] == fx["expected"]["text"]

    resp = client.post(
        "/documents/doc-1/merge",
        json={
            "base_version": "missing",
            "local_version": ids["local"],
            "remote_version": ids["remote"],
        },
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["category"] == "version-not-found"


def test_diagnostics_log_has_request_id_and_is_redacted(client, app, tmp_path):
    secret = "s3cr3t-token-value"
    fx = load_fixture("delete-vs-modify")
    resp = client.post(
        "/merge",
        json={
            "base": fx["base"],
            "local": fx["local"],
            "remote": fx["remote"] + secret + "\n",
        },
        headers={"X-Request-ID": "req-secret-1"},
    )
    assert resp.status_code == 200
    log_text = (tmp_path / "diagnostics.log").read_text(encoding="utf-8")
    assert log_text.strip(), "diagnostics log should not be empty"
    events = [json.loads(line) for line in log_text.strip().splitlines()]
    completed = [e for e in events if e["event"] == "merge.completed"]
    assert completed, "expected a merge.completed diagnostic"
    entry = completed[-1]
    # 请求标识与关键状态:为什么接受/拒绝/无法判定
    assert entry["request_id"] == "req-secret-1"
    assert entry["status"] == "conflicted"
    assert entry["conflict_kinds"] == ["delete-vs-modify"]
    assert entry["base"]["sha256"] and entry["base"]["chars"] > 0
    # 敏感内容只以脱敏指纹出现
    assert secret not in log_text
    assert "line2 modified" not in log_text
