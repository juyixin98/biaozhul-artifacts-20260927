"""独立配置模块与元数据事务层测试。"""
from __future__ import annotations

import os

import pytest

from colstats.config import load_config
from colstats.kernel import audit_file
from colstats.models import AuditResult, Finding, Severity
from colstats.store import MetadataStore


# ---- 配置


def test_load_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.delenv("COLSTATS_SERVICE_PORT", raising=False)
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.service.port == 8080
    assert cfg.audit.expose_values is False


def test_toml_overrides(tmp_path):
    (tmp_path / "c.toml").write_text(
        '[service]\nport = 9999\ndb_path = "x.db"\n'
        '[audit]\nexpose_values = true\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path / "c.toml")
    assert cfg.service.port == 9999
    assert cfg.service.db_path == "x.db"
    assert cfg.audit.expose_values is True


def test_env_overrides_toml(tmp_path, monkeypatch):
    (tmp_path / "c.toml").write_text('[service]\nport = 1111\n', encoding="utf-8")
    monkeypatch.setenv("COLSTATS_SERVICE_PORT", "2222")
    cfg = load_config(tmp_path / "c.toml")
    assert cfg.service.port == 2222


def test_env_bool_and_tuple(tmp_path, monkeypatch):
    monkeypatch.setenv("COLSTATS_AUDIT_EXPOSE_VALUES", "yes")
    monkeypatch.setenv(
        "COLSTATS_AUDIT_SUPPORTED_PHYSICAL_TYPES", "INT32, INT64"
    )
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.audit.expose_values is True
    assert cfg.audit.supported_physical_types == ("INT32", "INT64")


# ---- 元数据事务


def _fake_result(tmp_path):
    return AuditResult(
        path=str(tmp_path / "f.parquet"),
        verdict="REJECTED",
        audit_id="audit-123",
        findings=[
            Finding(
                code="MIN_MISMATCH",
                severity=Severity.ERROR,
                locator={"row_group": 0, "column": "x", "page": 2},
                message="min 不一致",
                expected={"value": 1},
                observed={"value": 999},
                request_id="req-abc",
            )
        ],
        trusted={"x": False},
        truncated_columns=[],
        summary={"num_errors": 1},
    )


def test_save_and_get_audit(tmp_path):
    store = MetadataStore(tmp_path / "a.db")
    store.save_audit(_fake_result(tmp_path), request_id="req-abc")
    got = store.get_audit("audit-123")
    assert got["verdict"] == "REJECTED"
    assert got["request_id"] == "req-abc"
    assert got["summary"] == {"num_errors": 1}
    assert got["trusted"] == {"x": False}
    f0 = got["findings"][0]
    assert f0["code"] == "MIN_MISMATCH"
    assert f0["locator"]["page"] == 2
    assert f0["observed"] == {"value": 999}


def test_transaction_atomicity(tmp_path):
    db = tmp_path / "a.db"
    store = MetadataStore(db)
    result = _fake_result(tmp_path)

    # 第二次写入用重复主键触发失败：头与 findings 必须一起回滚
    store.save_audit(result, request_id="r1")
    with pytest.raises(Exception):
        store.save_audit(result, request_id="r2")

    # 第一条仍在；没有产生孤儿 findings
    import sqlite3
    conn = sqlite3.connect(db)
    n_audits = conn.execute("SELECT COUNT(*) FROM audits").fetchone()[0]
    n_findings = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    conn.close()
    assert n_audits == 1
    assert n_findings == 1


def test_find_by_request_and_latest(tmp_path):
    store = MetadataStore(tmp_path / "a.db")
    r1 = _fake_result(tmp_path)
    r1.audit_id = "a1"
    r2 = _fake_result(tmp_path)
    r2.audit_id = "a2"
    store.save_audit(r1, request_id="same-req")
    store.save_audit(r2, request_id="same-req")
    rows = store.find_by_request("same-req")
    assert {r["audit_id"] for r in rows} == {"a1", "a2"}
    assert store.latest_for_path(str(tmp_path / "f.parquet"))["audit_id"] in (
        "a1", "a2"
    )
    assert store.get_audit("nope") is None


def test_fixture_audit_persists_roundtrip(tmp_path, fixture_models):
    store = MetadataStore(tmp_path / "f.db")
    result = audit_file(fixture_models["wrong"], request_id="req-x")
    store.save_audit(result, request_id="req-x")
    got = store.get_audit(result.audit_id)
    assert got["verdict"] == "REJECTED"
    codes = {f["code"] for f in got["findings"]}
    assert "NULL_COUNT_MISMATCH" in codes
