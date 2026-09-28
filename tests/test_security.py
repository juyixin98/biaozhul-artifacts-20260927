"""安全测试：输出不泄漏原始数据、令牌访问控制、静态加密、运行状态隔离。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from .conftest import ADMIN_HEADERS

RAW_MARKERS = ["10001", "10002", "12001", "Flu", "Cold"]


def test_no_raw_values_anywhere_in_risk_response(
        client, created_run, auth_headers):
    rid = created_run["run_id"]
    r = client.post(f"/runs/{rid}/evaluate?k=2&l=2",
                    json={"levels": {"zip": 0, "age": 0}},
                    headers=auth_headers)
    text = r.text
    for marker in RAW_MARKERS:
        assert marker not in text, f"风险响应泄漏原始值 {marker}"
    # 指纹存在且为不可逆十六进制
    fp = r.json()["classes"][0]["class_fingerprint"]
    assert isinstance(fp, str) and len(fp) == 32
    assert all(ch in "0123456789abcdef" for ch in fp)


def test_suggest_response_contains_no_raw_values(
        client, created_run, auth_headers):
    rid = created_run["run_id"]
    r = client.post(f"/runs/{rid}/suggest", json={"k": 2, "l": 2},
                    headers=auth_headers)
    for marker in RAW_MARKERS:
        assert marker not in r.text


def test_fingerprints_differ_across_runs(client, tiny_payload):
    """每运行独立 HMAC 密钥：相同原始类在不同运行的指纹必须不同。"""
    fps = []
    for _ in range(2):
        run = client.post("/runs", json=tiny_payload).json()
        r = client.post(
            f"/runs/{run['run_id']}/evaluate?k=2&l=2",
            json={"levels": {"zip": 0, "age": 0}},
            headers={"X-Run-Token": run["access_token"]},
        )
        fps.append(r.json()["classes"][0]["class_fingerprint"])
    assert fps[0] != fps[1]


def test_run_token_required_and_not_guessable(
        client, created_run, auth_headers):
    rid = created_run["run_id"]
    # 无令牌
    r = client.post(f"/runs/{rid}/evaluate?k=2&l=2",
                    json={"levels": {"zip": 0}})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"
    # 错令牌
    r = client.post(f"/runs/{rid}/evaluate?k=2&l=2",
                    json={"levels": {"zip": 0}},
                    headers={"X-Run-Token": "wrong-token"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "RUN_FORBIDDEN"


def test_admin_token_required_for_audit(client):
    r = client.get("/audit/events")
    assert r.status_code == 401
    r = client.get("/audit/events", headers={"X-Admin-Token": "nope"})
    assert r.status_code == 401
    r = client.get("/audit/events", headers=ADMIN_HEADERS)
    assert r.status_code == 200


def test_data_is_encrypted_at_rest(client, created_run, auth_headers, settings):
    """直接对 SQLite 文件做原始字符串扫描：原始值不得明文出现。"""
    rid = created_run["run_id"]
    # 触发一次评估后再扫
    client.post(f"/runs/{rid}/evaluate?k=2&l=2",
                json={"levels": {"zip": 0, "age": 0}},
                headers=auth_headers)
    db_path = Path(settings.storage.data_dir) / "runs" / f"{rid}.db"
    raw_bytes = db_path.read_bytes()
    for marker in [b"10001", b"Flu", b"Cold", b"12003"]:
        assert marker not in raw_bytes, f"运行库中发现明文 {marker!r}"
    # meta 表不含原始访问令牌，只存 64 位 SHA-256 摘要；数据包是 Fernet 密文
    conn = sqlite3.connect(str(db_path))
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    assert meta["data_blob"].startswith("67414141")  # hex('gAAA') Fernet 版本前缀
    assert "access_token" not in meta
    assert len(meta["token_sha256"]) == 64
    int(meta["token_sha256"], 16)
    conn.close()


def test_runs_are_isolated_files_and_cannot_share_state(
        client, tiny_payload):
    a = client.post("/runs", json=tiny_payload).json()
    b = client.post("/runs", json=tiny_payload).json()
    assert a["run_id"] != b["run_id"]
    # A 的令牌不能访问 B
    r = client.post(
        f"/runs/{b['run_id']}/suggest", json={"k": 2, "l": 2},
        headers={"X-Run-Token": a["access_token"]},
    )
    assert r.status_code == 403
    # 删除 A 不影响 B
    r = client.delete(f"/runs/{a['run_id']}",
                      headers={"X-Run-Token": a["access_token"]})
    assert r.status_code == 204
    r = client.post(
        f"/runs/{b['run_id']}/suggest", json={"k": 2, "l": 2},
        headers={"X-Run-Token": b["access_token"]},
    )
    assert r.status_code == 200


def test_audit_db_blocks_update_and_delete(client, created_run, auth_headers,
                                           settings):
    """审计表只追加：直接执行 UPDATE/DELETE 必须被触发器拒绝。"""
    rid = created_run["run_id"]
    client.post(f"/runs/{rid}/evaluate?k=2&l=2",
                json={"levels": {"zip": 0, "age": 0}},
                headers=auth_headers)
    conn = sqlite3.connect(settings.storage.audit_db)
    existing_id = conn.execute("SELECT MIN(id) FROM audit_events").fetchone()[0]
    assert existing_id is not None
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit_events SET status='X' WHERE id=?",
                     (existing_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_events WHERE id=?", (existing_id,))
    conn.close()


def test_audit_events_filter_by_run_and_have_correlation(
        client, created_run, auth_headers):
    rid = created_run["run_id"]
    client.post(f"/runs/{rid}/suggest", json={"k": 2, "l": 2},
                headers=auth_headers)
    res = client.get(f"/audit/events?run_id={rid}",
                     headers=ADMIN_HEADERS).json()
    assert res["total"] >= 1
    assert all(e["run_id"] == rid for e in res["events"])
    # 事件含 metric_version 与判定细节
    ev = [e for e in res["events"] if e["event"] == "suggest"][0]
    assert ev["metric_version"] == "1.0.0"
    assert ev["details"]["feasible"] is True


def test_run_summary_does_not_leak_data(client, created_run):
    rid = created_run["run_id"]
    r = client.get(f"/runs/{rid}")
    body = r.text
    for marker in RAW_MARKERS:
        assert marker not in body
    assert r.json()["row_count"] == 6
