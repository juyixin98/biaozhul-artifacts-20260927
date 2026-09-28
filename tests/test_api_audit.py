"""FastAPI 端到端：整段/流式一致、审计令牌、加密落库、请求身份关联。"""
from __future__ import annotations

from tests.fixtures import BANK_CARD, MOBILE, SAMPLE_LOG, TOKEN, chunks_of


def test_healthz(client):
    r = client.get("/api/v1/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["default_profile"] == "standard"
    assert "strict" in body["profiles"]


def test_redact_endpoint_concrete(client):
    r = client.post("/api/v1/redact",
                    json={"text": f"card {BANK_CARD} phone {MOBILE}"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["redacted_text"] == "card <BANK_CARD> phone <MOBILE>"
    assert BANK_CARD not in body["redacted_text"]
    assert body["request_id"].startswith("req-")
    assert body["profile"] == "standard"
    assert body["engine_version"]
    # 具体映射断言
    spans = [(m["rule_id"], m["original_span"]) for m in body["mappings"]]
    assert ("bank-card-16", [5, 21]) in spans
    assert ("cn-mobile-11", [28, 39]) in spans


def test_streaming_endpoint_equals_whole(client):
    whole = client.post("/api/v1/redact", json={"text": SAMPLE_LOG}).json()
    open_ = client.post("/api/v1/stream/open", json={}).json()
    rid = open_["request_id"]
    pieces = chunks_of(SAMPLE_LOG, [19, 37, 5, 64])
    collected = ""
    final_body = None
    for i, p in enumerate(pieces):
        is_last = i == len(pieces) - 1
        r = client.post("/api/v1/stream/chunk",
                        json={"request_id": rid, "chunk": p,
                              "final": is_last})
        assert r.status_code == 200
        b = r.json()
        collected += b["emitted_text"]
        if is_last:
            final_body = b
    assert final_body and final_body["finalized"] is True
    # 增量拼接 + 最终块的完整结果，等于整段结果
    final_full = final_body["result"]["redacted_text"]
    assert collected + final_full[len(collected):] == whole["redacted_text"]
    assert final_full == whole["redacted_text"]
    # 中间/最终都不含秘密
    for secret in (BANK_CARD, TOKEN, MOBILE):
        assert secret not in collected
        assert secret not in final_full


def test_stream_explicit_finalize(client):
    rid = client.post("/api/v1/stream/open", json={}).json()["request_id"]
    client.post("/api/v1/stream/chunk",
                json={"request_id": rid, "chunk": f"a {BANK_CARD}",
                      "final": False})
    r = client.post("/api/v1/stream/finalize", params={"request_id": rid})
    assert r.status_code == 200
    assert r.json()["redacted_text"] == f"a <BANK_CARD>"
    # 二次 finalize 必须报错（会话已关闭）
    r2 = client.post("/api/v1/stream/finalize", params={"request_id": rid})
    assert r2.status_code == 409
    assert r2.json()["detail"]["error_code"] == "SESSION_CLOSED"


def test_unknown_profile_404(client):
    r = client.post("/api/v1/redact",
                    json={"text": "x", "profile": "nope"})
    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "UNKNOWN_PROFILE"


def test_unknown_session_404(client):
    r = client.post("/api/v1/stream/chunk",
                    json={"request_id": "req-nonexistent", "chunk": "x"})
    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "SESSION_NOT_FOUND"


def test_request_too_large(client):
    r = client.post("/api/v1/redact", json={"text": "x" * 100_001})
    assert r.status_code == 413
    assert r.json()["detail"]["error_code"] == "INPUT_TOO_LARGE"


# --------------------------------------------------------------------- #
# 审计接口
# --------------------------------------------------------------------- #
def test_public_summary_no_output(client):
    body = client.post("/api/v1/redact",
                       json={"text": f"c {BANK_CARD}"}).json()
    rid = body["request_id"]
    r = client.get(f"/api/v1/requests/{rid}")
    assert r.status_code == 200
    summary = r.json()
    assert "redacted_output" not in summary
    assert summary["status"] == "ok"
    assert summary["original_length"] == len(f"c {BANK_CARD}")


def test_audit_requires_token(client):
    body = client.post("/api/v1/redact",
                       json={"text": f"c {BANK_CARD}"}).json()
    rid = body["request_id"]
    r = client.get(f"/api/v1/audit/requests/{rid}")
    assert r.status_code == 401
    assert r.json()["detail"]["error_code"] == "AUDIT_TOKEN_MISSING"
    r2 = client.get(f"/api/v1/audit/requests/{rid}",
                    headers={"X-Audit-Token": "wrong"})
    assert r2.status_code == 403


def test_audit_detail_and_encrypted_original(client):
    body = client.post("/api/v1/redact",
                       json={"text": f"card {BANK_CARD}"}).json()
    rid = body["request_id"]
    token = client.audit_token
    r = client.get(f"/api/v1/audit/requests/{rid}",
                   headers={"X-Audit-Token": token})
    assert r.status_code == 200
    detail = r.json()
    assert detail["redacted_output"] == "card <BANK_CARD>"
    # 映射不直接带原文
    assert "original" not in detail["mappings"][0]
    assert detail["mappings"][0]["rule_id"] == "bank-card-16"
    # 失败/不确定单列
    assert isinstance(detail["uncertainties"], list)
    # 关键步骤可解释
    kinds = {e["kind"] for e in detail["events"]}
    assert {"CHUNK_RECEIVED", "FINALIZED"} <= kinds

    # 单条原文解密接口
    idx = 0
    ro = client.get(
        f"/api/v1/audit/requests/{rid}/mappings/{idx}/original",
        headers={"X-Audit-Token": token})
    assert ro.status_code == 200
    assert ro.json()["original"] == BANK_CARD
    assert ro.json()["original_span"] == [5, 21]


def test_sqlite_file_contains_no_plaintext_secret(settings, client):
    """库文件中不得出现明文秘密（原文必须加密）。"""
    client.post("/api/v1/redact", json={"text": f"secret {TOKEN}"})
    # 关闭连接并 checkpoint，使 WAL 落盘后再检查物理文件
    client.app.state.store.close()
    import sqlite3
    con = sqlite3.connect(settings.db_path)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.commit()
    con.close()
    raw = open(settings.db_path, "rb").read()
    assert TOKEN.encode() not in raw
    # 结构化步骤事件（不含原文）正常落库
    assert b"CHUNK_RECEIVED" in raw
    # 加密原文存的是 Fernet token，不以明文出现
    assert b"SYN0123456789" not in raw
