"""Shared test fixtures.

Each test session gets an isolated temporary fixture DB created *directly via
sqlite3* from independent DDL — i.e. the expected schema is authored in the
test suite, never derived from the code under test. The reviewer then opens
that file read-only, which is exactly what the isolation tests assert.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from sqlguard.core.policy import (
    ParamPolicy,
    Policy,
    SlotPolicy,
)
from sqlguard.logging_setup import configure_logging
from sqlguard.state.audit import AuditStore
from sqlguard.state.fixture import ReadOnlyFixture, create_fixture
from sqlguard.service import ReviewService

# DDL authored independently of sqlguard internals.
FIXTURE_SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE TABLE customers (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT, created_at TEXT NOT NULL
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE VIEW active_users AS
SELECT id, name, email FROM users WHERE status = 'active';
"""

FIXTURE_SEED = """
INSERT INTO users VALUES
 (1,'Ada','ada@example.test','admin','active','2026-01-04T09:00:00Z'),
 (2,'Alan','alan@example.test','member','active','2026-02-11T12:30:00Z'),
 (3,'Grace','grace@example.test','admin','disabled','2026-03-21T08:15:00Z');
INSERT INTO customers VALUES (1,'Babbage Corp','contact@babbage.test','2026-01-10T00:00:00Z');
INSERT INTO orders VALUES (100,1,4200,'open','2026-05-01T10:00:00Z');
"""


@pytest.fixture()
def fixture_path(tmp_path: Path) -> Path:
    return create_fixture(tmp_path / "fixture.db", FIXTURE_SCHEMA, FIXTURE_SEED)


@pytest.fixture()
def ro_fixture(fixture_path: Path) -> ReadOnlyFixture:
    f = ReadOnlyFixture(fixture_path)
    yield f
    f.close()


@pytest.fixture()
def policy() -> Policy:
    return Policy(
        allowed_statement_types=frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
        slots={
            "user_relation": SlotPolicy(
                allowed_identifiers=frozenset({"users", "customers"}),
                require_in_schema=True, scope="relation"),
            "sort_col": SlotPolicy(
                allowed_identifiers=frozenset({"id", "name", "email", "created_at"}),
                default="id", require_in_schema=False, scope="sort"),
            "sort_dir": SlotPolicy(
                allowed_identifiers=frozenset({"ASC", "DESC"}),
                default="ASC", require_in_schema=False, scope="sort",
                quote=False),
        },
        params={
            "*": ParamPolicy(allow_array=True),
            "role": ParamPolicy(
                allowed_values=frozenset({"admin", "member", "guest"})),
            "status": ParamPolicy(
                allowed_values=frozenset({"active", "disabled", "pending"})),
        },
        writable_tables=frozenset({"users", "orders"}),
        known_tables=frozenset({"users", "customers", "orders", "active_users"}),
        require_where_for_update_delete=True,
    )


@pytest.fixture()
def audit(tmp_path: Path) -> AuditStore:
    store = AuditStore(tmp_path / "audit.db", key=b"0" * 32)
    yield store
    store.close()


@pytest.fixture()
def service(policy: Policy, ro_fixture: ReadOnlyFixture,
            audit: AuditStore) -> ReviewService:
    logger = configure_logging("WARNING")
    return ReviewService(policy, ro_fixture, audit, logger)
