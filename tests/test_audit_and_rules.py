"""审计哈希链 + 签名验证测试，及规则加载器校验测试。"""

from __future__ import annotations

import json

import pytest

from conftest import POLICY_PATH, ZONE_PATH, ok, redirect
from safeproxy.audit.store import AuditLog
from safeproxy.errors import AuditChainBrokenError, PolicyFileError, ZoneFileError
from safeproxy.net.resolver import parse_zone
from safeproxy.rules.loader import load_policy


@pytest.fixture
def audit(tmp_path):
    return AuditLog(str(tmp_path / "audit.sqlite3"))


def _event(run_id, verdict="deny", code="E_POLICY_DENY"):
    return {
        "run_id": run_id,
        "final_verdict": verdict,
        "url": "http://x/",
        "status_code": None,
        "pinned": [],
        "connected_peer": None,
        "error": {"code": code, "category": "policy_deny"},
        "hops": [{"hop": 1, "stage": "policy", "verdict": "deny"}],
    }


def test_chain_verifies_on_clean_log(audit):
    for i in range(5):
        audit.record(_event(f"r{i}"))
    res = audit.verify_chain()
    assert res["ok"] is True and res["records"] == 5


def test_tamper_detected(audit):
    for i in range(3):
        audit.record(_event(f"r{i}"))
    # 直接篡改数据库里的一条记录内容（模拟落库后被改）
    import sqlite3

    conn = sqlite3.connect(audit._db_path)
    row = conn.execute("SELECT record_json FROM audit_runs WHERE run_id='r1'").fetchone()
    tampered = json.loads(row[0])
    tampered["url"] = "http://attacker/"
    conn.execute(
        "UPDATE audit_runs SET record_json=? WHERE run_id='r1'",
        (json.dumps(tampered, sort_keys=True),),
    )
    conn.commit()
    conn.close()

    with pytest.raises(AuditChainBrokenError) as ei:
        audit.verify_chain()
    assert ei.value.details["run_id"] == "r1"


def test_swap_rows_detected(audit):
    for i in range(3):
        audit.record(_event(f"r{i}"))
    import sqlite3

    conn = sqlite3.connect(audit._db_path)
    h0 = conn.execute("SELECT chain_hash FROM audit_runs WHERE run_id='r0'").fetchone()[0]
    h2 = conn.execute("SELECT chain_hash FROM audit_runs WHERE run_id='r2'").fetchone()[0]
    conn.execute("UPDATE audit_runs SET chain_hash=? WHERE run_id='r0'", (h2,))
    conn.execute("UPDATE audit_runs SET chain_hash=? WHERE run_id='r2'", (h0,))
    conn.commit()
    conn.close()
    with pytest.raises(AuditChainBrokenError):
        audit.verify_chain()


def test_run_id_isolation(audit, factory):
    kernel, connector = factory(
        routes={"/ok": ok(b"abc")}, audit=audit
    )
    r1 = kernel.fetch("http://127.0.0.1:18080/ok")
    r2 = kernel.fetch("http://169.254.169.254/")
    got1 = audit.get_run(r1["run_id"])
    got2 = audit.get_run(r2["run_id"])
    assert got1["verdict"] == "allow"
    assert got2["verdict"] == "deny"
    assert got1["run_id"] != got2["run_id"]
    assert audit.get_run("nonexistent") is None


def test_audit_retains_replay_fields(audit, factory):
    kernel, _ = factory(routes={"/redirect-meta": redirect("http://metadata.example/")}, audit=audit)
    result = kernel.fetch("http://127.0.0.1:18080/redirect-meta")
    row = audit.get_run(result["run_id"])
    record = json.loads(row["record_json"])
    # 可重放关键中间状态：运行编号、每跳、判断理由
    assert "run_id" in record
    assert any(h["stage"] == "dns" for h in record["hops"])
    deny = next(h for h in record["hops"] if h["stage"] == "policy" and h["verdict"] == "deny")
    assert deny["matched"]["rule_id"] == "deny-metadata-ipv4"


# ---------------------------------------------------------------------------
# 规则 / zone 文件校验
# ---------------------------------------------------------------------------
def test_load_valid_policy_and_zone():
    bundle = load_policy(str(POLICY_PATH))
    assert bundle.default_action == "deny"
    assert bundle.mixed_set_mode == "deny_if_any_forbidden"
    table = parse_zone(str(ZONE_PATH))
    assert "metadata.example" in table
    assert table["mapped.example"][0].literal == "127.0.0.1"


def test_policy_rejects_bad_file(tmp_path):
    bad = tmp_path / "p.json"
    bad.write_text(json.dumps({"version": 1}), encoding="utf-8")
    with pytest.raises(PolicyFileError) as ei:
        load_policy(str(bad))
    assert "缺少必需字段" in ei.value.message


def test_policy_rejects_unknown_action(tmp_path):
    data = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    data["default_action"] = "permit"
    p = tmp_path / "p.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PolicyFileError):
        load_policy(str(p))


def test_policy_rejects_implicit_mixed_mode(tmp_path):
    data = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    data["mixed_address_set"] = "first_only"
    p = tmp_path / "p.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PolicyFileError) as ei:
        load_policy(str(p))
    assert "mixed_address_set" in ei.value.message


def test_zone_rejects_bad_line(tmp_path):
    z = tmp_path / "bad.zone"
    z.write_text("not-a-valid-line\n", encoding="utf-8")
    with pytest.raises(ZoneFileError):
        parse_zone(str(z))


def test_port_interpolation(monkeypatch):
    from safeproxy.rules.loader import _interpolate

    monkeypatch.setenv("MY_TEST_PORT", "9999")
    assert _interpolate("p-${MY_TEST_PORT}-${MISSING:-7}") == "p-9999-7"
    assert _interpolate("${MISSING:-18080}") == "18080"
