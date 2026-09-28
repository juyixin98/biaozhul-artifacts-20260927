"""集成：FastAPI 端到端——更新、证明、独立验证器、旧根历史证明、批=逐条根。"""
from __future__ import annotations


from app.core.smt import SparseMerkleTree
from app.core.store import InMemoryStore
from app.coding.params import TreeParams


def k(suffix: int) -> str:
    return (b"\x22" * 31 + bytes([suffix])).hex()


def batch(client, pairs, idem=None):
    def _hexish(v):
        if v is None:
            return None
        if isinstance(v, (bytes, bytearray)):
            return bytes(v).hex()
        return v
    body = {"updates": [{"key": _hexish(key), "value": _hexish(val)}
                        for key, val in pairs]}
    if idem:
        body["idempotency_key"] = idem
    resp = client.post("/updates", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_health_and_genesis(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["version"] == 0
    r0 = client.get("/root").json()
    assert r0["version"] == 0 and len(r0["root"]) == 64
    # v0 是确定性空根（与内核常量一致）
    assert r0["root"] == TreeParams(32, 256).empty_root.hex()


def test_update_membership_proof_and_verify(client):
    r = batch(client, [(k(1), b"one"), (k(2), b"two")])
    assert r["version"] == 1 and r["changed"] == 2

    proof_resp = client.get(f"/proof/{k(1)}").json()
    assert proof_resp["exists"] is True and proof_resp["value"] == b"one".hex()

    verify_resp = client.post("/verify", json={
        "proof": proof_resp["proof"],
        "expect_membership": True,
        "expect_value": b"one".hex(),
    })
    assert verify_resp.status_code == 200, verify_resp.text
    body = verify_resp.json()
    assert body["decision"] == "ACCEPT"
    assert body["reason"] == "MEMBERSHIP_VERIFIED"


def test_non_membership_proof_accepted(client):
    batch(client, [(k(1), b"one")])
    proof_resp = client.get(f"/proof/{k(99)}").json()
    assert proof_resp["exists"] is False
    verify_resp = client.post("/verify", json={
        "proof": proof_resp["proof"], "expect_membership": False,
    })
    assert verify_resp.status_code == 200
    assert verify_resp.json()["reason"] == "NON_MEMBERSHIP_VERIFIED"


def test_member_proof_for_absent_expectation_rejected_kind_mismatch(client):
    proof_resp = client.get(f"/proof/{k(7)}").json()  # 空树，非成员
    resp = client.post("/verify", json={
        "proof": proof_resp["proof"], "expect_membership": True,
    })
    assert resp.status_code == 422
    assert resp.json()["reason"] == "KIND_MISMATCH"


def test_tampered_root_rejected_with_concrete_category(client):
    batch(client, [(k(1), b"one")])
    proof = client.get(f"/proof/{k(1)}").json()["proof"]
    proof["root"] = "00" * 32
    resp = client.post("/verify", json={"proof": proof, "expect_membership": True})
    assert resp.status_code == 422
    body = resp.json()
    assert body["decision"] == "REJECT"
    assert body["reason"] == "ROOT_MISMATCH"
    assert body["claimed_root"] == "00" * 32
    assert body["recomputed_root"] != "00" * 32  # 诊断给出关键状态


def test_malformed_envelope_is_inconclusive_not_rejected(client):
    resp = client.post("/verify", json={"proof": {"unexpected": 1}})
    assert resp.status_code == 400
    body = resp.json()
    assert body["reason"] == "ENVELOPE_MALFORMED"
    assert body["request_id"]  # 诊断带请求标识


def test_request_id_roundtrip_and_custom(client):
    resp = client.get("/health", headers={"X-Request-ID": "fixed-req-123"})
    assert resp.headers["X-Request-ID"] == "fixed-req-123"


def test_empty_value_distinct_from_absent_over_api(client):
    batch(client, [(k(3), b"")])  # 空字节串值
    present = client.get(f"/value/{k(3)}").json()
    assert present["exists"] is True and present["value"] == ""
    absent = client.get(f"/value/{k(4)}").json()
    assert absent["exists"] is False and absent["value"] is None


def test_batch_root_equals_one_by_one_roots(client):
    """比较批更新与逐个更新最终根（用独立内核实例对照）。"""
    pairs = [(bytes.fromhex(k(i)), f"value-{i}".encode()) for i in range(1, 9)]
    r = batch(client, pairs)
    batch_root = r["root"]

    params = TreeParams(32, 256)

    # 独立逐条链（批中顺序）
    t_forward = SparseMerkleTree(InMemoryStore(), params)
    root_f = params.empty_root
    for key, val in pairs:
        root_f = t_forward.update(root_f, key, val)
    assert root_f.hex() == batch_root

    # 另一棵树，乱序逐条，根也必须相同（顺序无关）
    t_reverse = SparseMerkleTree(InMemoryStore(), params)
    root_r = params.empty_root
    for key, val in reversed(pairs):
        root_r = t_reverse.update(root_r, key, val)
    assert root_r.hex() == batch_root


def test_idempotency_same_key_same_payload_returns_same_version(client):
    body = {"updates": [{"key": k(1), "value": b"x".hex()}], "idempotency_key": "idem-1"}
    r1 = client.post("/updates", json=body)
    r2 = client.post("/updates", json=body)
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["version"] == r2.json()["version"]
    assert r2.json()["idempotent_replay"] is True


def test_idempotency_same_key_different_payload_conflict(client):
    client.post("/updates", json={
        "updates": [{"key": k(1), "value": b"x".hex()}], "idempotency_key": "idem-2",
    })
    resp = client.post("/updates", json={
        "updates": [{"key": k(1), "value": b"y".hex()}], "idempotency_key": "idem-2",
    })
    assert resp.status_code == 409
    assert resp.json()["reason"] == "INCONCLUSIVE"
    assert resp.json()["detail"]["existing_version"] == 1


def test_historical_root_verifies_old_proof(client):
    """旧根仍可验证历史证明：v1 写值，v2 改值，v1 的证明仍绑定 v1 根并可核验。"""
    batch(client, [(k(1), b"v1-value")], idem="h1")
    v1 = client.get("/root").json()["version"]
    old_proof = client.get(f"/proof/{k(1)}?version={v1}").json()["proof"]
    assert old_proof["root"] == client.get("/root").json()["root"]

    batch(client, [(k(1), b"v2-value")], idem="h2")
    v2 = client.get("/root").json()["version"]
    assert v2 == v1 + 1

    # v2 当前证明值不同
    current = client.get(f"/proof/{k(1)}").json()
    assert current["value"] == b"v2-value".hex()

    # v1 历史证明仍可取（内容寻址节点未删除）且在 v1 根上核验通过
    resp = client.post("/verify", json={
        "proof": old_proof, "expect_membership": True, "expect_value": b"v1-value".hex(),
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["reason"] == "MEMBERSHIP_VERIFIED"
    # 用旧证明配新期望值会被拒绝
    resp2 = client.post("/verify", json={
        "proof": old_proof, "expect_membership": True, "expect_value": b"v2-value".hex(),
    })
    assert resp2.json()["reason"] == "VALUE_MISMATCH"


def test_version_404_and_malformed_key(client):
    assert client.get("/versions/999").status_code == 404
    assert client.get("/versions/999").json()["reason"] == "UNKNOWN_ROOT"
    resp = client.get("/value/zz")
    assert resp.status_code == 400 and resp.json()["reason"] == "ENCODING_ERROR"
    resp = client.post("/updates", json={"updates": [{"key": "ab", "value": "01"}]})
    assert resp.status_code == 400  # 键宽错误（32 字节）
