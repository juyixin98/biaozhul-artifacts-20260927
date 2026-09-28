"""State-isolation tests: the fixture connection must be physically
read-only, and the schema snapshot must be stable."""

from __future__ import annotations

import sqlite3

import pytest

from sqlguard.isolation import (
    open_readonly_connection, snapshot_schema, SchemaUnavailable,
)


def test_readonly_connection_rejects_write(fixture_db):
    conn = open_readonly_connection(fixture_db)
    # The URI forces read-only/immutable; the authorizer denies anything
    # that is not SELECT/PRAGMA/READ. Both surface as sqlite3 errors.
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("CREATE TABLE evil (x INTEGER)")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("INSERT INTO users (id) VALUES (999)")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("ATTACH DATABASE '/tmp/evil-attach.db' AS x")
    conn.close()


def test_readonly_select_works(fixture_db):
    conn = open_readonly_connection(fixture_db)
    rows = conn.execute("SELECT count(*) FROM users").fetchone()
    assert rows[0] == 3
    conn.close()


def test_snapshot_contains_declared_tables(fixture_db):
    snap = snapshot_schema(fixture_db)
    assert {"users", "orders", "products", "audit_events"} <= set(snap.tables)
    assert "email" in snap.tables["users"]
    assert len(snap.digest) == 16


def test_snapshot_is_deterministic(fixture_db):
    a = snapshot_schema(fixture_db)
    b = snapshot_schema(fixture_db)
    assert a.digest == b.digest


def test_missing_fixture_raises():
    with pytest.raises(SchemaUnavailable):
        snapshot_schema("/nonexistent/definitely-missing.db")


def test_snapshot_has_column_and_table_helpers(fixture_db):
    snap = snapshot_schema(fixture_db)
    assert snap.has_column("orders", "total_cents")
    assert not snap.has_column("orders", "nope")
    assert not snap.has_table("nope")
