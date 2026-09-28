"""审计哈希链：追加、校验、篡改检测、失败状态不记成功。"""

from __future__ import annotations

import json

import pytest

from app.security.audit import AuditLog, verify_chain
from app.security.crypto import derive_signing_key


@pytest.fixture()
def audit(tmp_path):
    key = derive_signing_key(b"test-master-key")
    return AuditLog(tmp_path / "audit.jsonl", key)


def test_chain_verifies_clean(audit):
    audit.append("a", "succeeded")
    audit.append("b", "failed", run_id="run_x", detail={"code": "K_UNREACHABLE"})
    result = audit.verify_chain()
    assert result["ok"] is True
    assert result["records"] == 2


def test_records_link_via_prev_hash(audit):
    audit.append("a", "succeeded")
    audit.append("b", "succeeded")
    recs = audit.read_all()
    assert recs[0]["prev_hash"] == "0" * 64
    assert recs[1]["prev_hash"] == recs[0]["record_hash"]
    assert recs[1]["record_hash"] != recs[0]["record_hash"]


def test_tampering_is_detected(audit):
    audit.append("a", "succeeded", detail={"v": 1})
    audit.append("b", "failed")
    path = audit.path

    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["detail"] = {"v": 999}  # 篡改内容
    lines[0] = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_chain(path, audit.key)
    assert result["ok"] is False
    assert any("HMAC mismatch" in e["error"] for e in result["errors"])


def test_deleted_record_breaks_chain(audit):
    for i in range(3):
        audit.append(f"a{i}", "succeeded")
    path = audit.path
    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = verify_chain(path, audit.key)
    assert result["ok"] is False
    assert any("chain broken" in e["error"] or "seq gap" in e["error"] for e in result["errors"])


def test_unknown_status_rejected(audit):
    with pytest.raises(ValueError):
        audit.append("a", "maybe")


def test_service_logs_failure_not_success_for_unreachable(state):
    # 通过 service 触发一次 K 不可达
    from tests.conftest import load_fixture, make_payload

    fx = load_fixture("unique_signatures")
    state.service.analyze(make_payload(fx, 2, 1))
    recs = state.audit.read_all()
    analyze_records = [r for r in recs if r["action"] == "analyze"]
    assert analyze_records
    last = analyze_records[-1]
    assert last["status"] == "failed"
    assert last["detail"]["failure_code"] == "K_UNREACHABLE"
    assert last["run_id"].startswith("run_")
