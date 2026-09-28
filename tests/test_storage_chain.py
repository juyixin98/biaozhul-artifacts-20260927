"""状态隔离、加密落盘、审计事件哈希链与资源耗尽。"""
from __future__ import annotations

import json

import pytest

from app.errors import ComputationFailureError, ErrorCode, ResourceExhaustedError
from app.models import Evidence, Policy
from app.parser import sha256_hex

SECRET_AUTHZ = "Bearer synthetic-alice-token-0001"
SECRET_COOKIE = "sid=synthetic-session-aaaa"
SECRET_BODY = '{"user":"alice","balance":100}'


def _evidence(eid="e1"):
    return Evidence.model_validate({
        "id": eid, "source": "synthetic_fixture",
        "request": {"method": "get", "scheme": "https", "host": "h", "path": "/me",
                    "headers": {"Authorization": SECRET_AUTHZ, "Cookie": SECRET_COOKIE}},
        "response": {"status": 200,
                     "headers": {"Cache-Control": "max-age=10", "Set-Cookie": "sid=new"},
                     "body": SECRET_BODY, "body_sha256": sha256_hex(SECRET_BODY)},
    })


def _policy():
    return Policy(name="p", cache_scope="shared",
                  include_authorization=True, include_cookie=True,
                  allow_storing_authorization_response=True,
                  allow_storing_cookie_response=True,
                  respect_response_vary=False)


def test_secrets_encrypted_at_rest(storage):
    storage.create_run("run-secret")
    storage.set_policy("run-secret", _policy())
    storage.add_evidence("run-secret", [_evidence()])

    raw = storage._conn.execute(
        "SELECT payload_json FROM evidence WHERE run_id=?", ("run-secret",)
    ).fetchone()[0]
    assert SECRET_AUTHZ not in raw
    assert SECRET_COOKIE not in raw
    assert SECRET_BODY not in raw
    assert "gAAAAA" in raw  # Fernet token 前缀

    # 正常解密读回
    evs = storage.list_evidence("run-secret")
    assert evs[0].request.headers["Authorization"] == SECRET_AUTHZ
    assert evs[0].request.headers["Cookie"] == SECRET_COOKIE
    assert evs[0].response.body == SECRET_BODY


def test_chain_records_replayable_states_and_verifies(storage):
    storage.create_run("run-chain", label="lab")
    storage.set_policy("run-chain", _policy())
    storage.add_evidence("run-chain", [_evidence()])
    from app.kernel import analyze
    result = analyze(storage.get_policy("run-chain"),
                     storage.list_evidence("run-chain"))
    storage.save_analysis("run-chain", "current", result)

    verdict = storage.verify_chain("run-chain")
    assert verdict["ok"] is True
    types = [e["event_type"] for e in storage.read_events("run-chain")]
    assert types == ["run_created", "policy_set", "evidence_added", "analysis_saved"]
    # 相邻事件的哈希链接
    events = storage.read_events("run-chain")
    for prev, cur in zip(events, events[1:]):
        assert cur["prev_hash"] == prev["entry_hash"]
    # 事件载荷里身份头必须脱敏
    assert events[2]["payload"]["request"]["headers"]["Authorization"].startswith(
        "<redacted")


def test_chain_detects_tampering(storage):
    storage.create_run("run-tamper")
    storage.set_policy("run-tamper", _policy())
    storage.add_evidence("run-tamper", [_evidence()])
    # 攻击者直接改库
    storage._conn.execute(
        "UPDATE events SET payload_json=? WHERE run_id=? AND seq=2",
        (json.dumps({"event_type": "policy_set", "tampered": True}), "run-tamper"),
    )
    storage._conn.commit()
    verdict = storage.verify_chain("run-tamper")
    assert verdict["ok"] is False
    assert verdict["first_mismatch_seq"] == 2  # 被篡改条目自身的 entry_hash 即不匹配


def test_wrong_master_key_cannot_decrypt(tmp_path):
    from app.storage import AuditStorage
    s1 = AuditStorage(tmp_path / "k.sqlite3", "key-one")
    s1.create_run("r")
    s1.set_policy("r", _policy())
    s1.add_evidence("r", [_evidence()])
    s1.close()

    s2 = AuditStorage(tmp_path / "k.sqlite3", "key-two")
    with pytest.raises(ComputationFailureError) as ei:
        s2.list_evidence("r")
    assert ei.value.code == ErrorCode.CIPHERTEXT_INVALID
    assert ei.value.category == "computation_failure"


def test_too_many_evidence_is_resource_exhausted(storage, monkeypatch):
    monkeypatch.setattr("app.storage.MAX_EVIDENCE_PER_RUN", 2)
    storage.create_run("r")
    storage.set_policy("r", _policy())
    storage.add_evidence("r", [_evidence("e1"), _evidence("e2")])
    with pytest.raises(ResourceExhaustedError) as ei:
        storage.add_evidence("r", [_evidence("e3")])
    assert ei.value.code == ErrorCode.TOO_MANY_EVIDENCE
    assert ei.value.category == "resource_exhausted"
    assert ei.value.details["limit"] == 2


def test_body_too_large_is_resource_exhausted(storage, monkeypatch):
    monkeypatch.setattr("app.storage.MAX_BODY_BYTES", 16)
    storage.create_run("r")
    storage.set_policy("r", _policy())
    big = Evidence.model_validate({
        "id": "big",
        "request": {"method": "get", "scheme": "https", "host": "h", "path": "/",
                    "headers": {}},
        "response": {"status": 200,
                     "headers": {"Cache-Control": "max-age=10"},
                     "body": "x" * 64, "body_sha256": sha256_hex("x" * 64)},
    })
    with pytest.raises(ResourceExhaustedError) as ei:
        storage.add_evidence("r", [big])
    assert ei.value.code == ErrorCode.EVIDENCE_TOO_LARGE
    # 超限批次完全回滚
    assert storage.get_run("r")["evidence_count"] == 0


def test_batch_rollback_on_hash_mismatch(storage):
    storage.create_run("r")
    storage.set_policy("r", _policy())
    good = _evidence("good")
    bad = _evidence("bad")
    object.__setattr__(bad.response, "body_sha256", "f" * 64)
    with pytest.raises(ComputationFailureError):
        storage.add_evidence("r", [good, bad])
    assert storage.get_run("r")["evidence_count"] == 0
