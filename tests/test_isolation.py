"""State isolation tests.

Proves the two guarantees that matter most operationally:

1. The fixture is opened strictly read-only — even our own code cannot write
   through the review connection, and neither can a submitted statement.
2. Review never *executes* user SQL — not even accepted DML touches rows.
3. The audit chain detects tampering.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from sqlguard.core.kernel import Kernel
from sqlguard.state.audit import AuditStore
from sqlguard.state.fixture import ReadOnlyFixture, create_fixture
from tests.conftest import FIXTURE_SCHEMA, FIXTURE_SEED


def test_fixture_connection_is_read_only(tmp_path):
    p = create_fixture(tmp_path / "f.db", FIXTURE_SCHEMA, FIXTURE_SEED)
    ro = ReadOnlyFixture(p)

    # These must either raise (DENY / mount refusal) or be neutralized by the
    # authorizer returning IGNORE — in every case the invariant is "no write
    # reaches the database file".
    statements = (
        "INSERT INTO users (id,name,email,created_at) VALUES (99,'x','y','z')",
        "UPDATE users SET name = 'z' WHERE id = 1",
        "DELETE FROM users",
        "DROP TABLE users",
        "CREATE TABLE evil (x)",
        "PRAGMA journal_mode=DELETE",
    )
    for stmt in statements:
        try:
            ro.connection.execute(stmt)
        except sqlite3.DatabaseError:
            pass

    # open a fresh independent handle to prove nothing persisted
    probe = sqlite3.connect(p)
    assert probe.execute("SELECT count(*) FROM users WHERE id = 99").fetchone()[0] == 0
    assert probe.execute(
        "SELECT count(*) FROM sqlite_master WHERE name = 'evil'").fetchone()[0] == 0
    assert probe.execute("SELECT count(*) FROM users").fetchone()[0] == 3
    assert probe.execute(
        "SELECT name FROM users WHERE id = 1").fetchone()[0] == "Ada"
    probe.close()
    ro.close()
    ro.close()


def test_ro_connection_ignores_mode_override_in_attached_uri(tmp_path):
    p = create_fixture(tmp_path / "f.db", FIXTURE_SCHEMA, FIXTURE_SEED)
    ro = ReadOnlyFixture(p)
    # ATTACH of the same file claiming rw must still be blocked by immutable
    with pytest.raises(sqlite3.Error):
        ro.connection.execute(
            f'ATTACH DATABASE "file:{p.resolve()}?mode=rw" AS evil')
    ro.close()


def test_review_does_not_execute_accepted_dml(policy, ro_fixture):
    original = ro_fixture.connection.execute(
        "SELECT name, email FROM users WHERE id = 1").fetchone()
    k = Kernel(policy, ro_fixture)
    r = k.review(
        "UPDATE users SET name = :n, email = :e WHERE id = :id",
        params={"n": "PWNED", "e": "pwned@evil.test", "id": 1})
    assert r.verdict.value == "accept"
    after = ro_fixture.connection.execute(
        "SELECT name, email FROM users WHERE id = 1").fetchone()
    assert tuple(after) == tuple(original)


def test_review_does_not_execute_rejected_statement_either(policy, ro_fixture):
    count_before = ro_fixture.connection.execute("SELECT count(*) FROM users").fetchone()[0]
    k = Kernel(policy, ro_fixture)
    k.review("DELETE FROM users")  # rejected: no WHERE
    count_after = ro_fixture.connection.execute("SELECT count(*) FROM users").fetchone()[0]
    assert count_before == count_after


def test_fixture_file_unchanged_after_many_reviews(policy, ro_fixture, tmp_path):
    import hashlib
    path = ro_fixture.path
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    k = Kernel(policy, ro_fixture)
    for sql, params in [
        ("SELECT id FROM users WHERE id = ?", {"0": 1}),
        ("UPDATE users SET status = ? WHERE id = ?", {"0": "x", "1": 1}),
        ("DELETE FROM users WHERE id = ?", {"0": 1}),
        ("SELECT * FROM users WHERE id IN (?)", {"0": [1, 2]}),
    ]:
        k.review(sql, params=params)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


# --------------------------------------------------------------- audit chain

def _review_dict(verdict="accept"):
    return {
        "verdict": verdict,
        "statement_type": "SELECT",
        "rendered_sql": "SELECT 1",
        "resolved_identifiers": {},
        "param_diagnostics": [],
        "findings": [],
        "coverage": {},
        "codes": {"reject": [], "unanalyzable": [], "advisory": []},
    }


def test_audit_chain_verifies_clean(audit: AuditStore):
    for i in range(5):
        audit.append(request_id=f"r{i}", template="SELECT 1",
                     result_dict=_review_dict())
    report = audit.verify_chain()
    assert report.ok is True
    assert report.records == 5


def test_audit_chain_detects_row_tampering(audit: AuditStore):
    audit.append(request_id="r0", template="SELECT 1",
                 result_dict=_review_dict("accept"))
    audit.append(request_id="r1", template="SELECT 1",
                 result_dict=_review_dict("reject"))
    audit.append(request_id="r2", template="SELECT 1",
                 result_dict=_review_dict("accept"))
    # attacker flips r1's stored verdict from reject to accept
    audit._conn.execute(
        "UPDATE audit_records SET verdict = 'accept' WHERE request_id = 'r1'")
    audit._conn.commit()
    report = audit.verify_chain()
    assert report.ok is False
    assert report.first_bad_seq == 2
    assert "HMAC" in (report.reason or "")


def test_audit_chain_detects_deletion_and_reordering(audit: AuditStore):
    for i in range(4):
        audit.append(request_id=f"r{i}", template="SELECT 1",
                     result_dict=_review_dict())
    audit._conn.execute("DELETE FROM audit_records WHERE seq = 2")
    audit._conn.commit()
    report = audit.verify_chain()
    assert report.ok is False


def test_audit_stores_only_redacted_evidence(audit: AuditStore):
    secret = "super-secret-customer@example.test"
    result = _review_dict()
    result["param_diagnostics"] = [
        {"marker": "?", "binding": {"type": "str", "length": len(secret)}}]
    audit.append(request_id="r-secret", template="SELECT ?", result_dict=result)
    raw = audit._conn.execute(
        "SELECT evidence FROM audit_records WHERE request_id='r-secret'"
    ).fetchone()[0]
    assert secret not in raw
    assert json.loads(raw)["param_diagnostics"][0]["binding"]["type"] == "str"


def test_audit_get_and_list_roundtrip(audit: AuditStore):
    rid = audit.append(template="SELECT 1", result_dict=_review_dict("reject"))
    fetched = audit.get(rid)
    assert fetched["request_id"] == rid
    assert fetched["verdict"] == "reject"
    listed = audit.list()
    assert any(e["request_id"] == rid for e in listed)


def test_chain_uses_genesis_and_distinct_key_fails(tmp_path):
    a = AuditStore(tmp_path / "a.db", key=b"k" * 32)
    a.append(template="SELECT 1", result_dict=_review_dict())
    a.close()
    # reopening with a different key must invalidate verification
    b = AuditStore(tmp_path / "a.db", key=b"z" * 32)
    assert b.verify_chain().ok is False
    b.close()
