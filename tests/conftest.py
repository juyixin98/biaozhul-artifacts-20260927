"""Shared pytest fixtures: local synthetic data only.

Builds an on-disk SQLite database with two composite-key tables and returns
canonical request pieces. No network, no production data.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

import pyarrow as pa
import pytest

from merge_engine.service import MergeService


@pytest.fixture()
def tmp_dir(tmp_path: str) -> str:
    return str(tmp_path)


@pytest.fixture()
def service(tmp_dir: str) -> MergeService:
    db_path = os.path.join(tmp_dir, "merge-test.db")
    log_path = os.path.join(tmp_dir, "logs", "merge-runs.jsonl")
    # TestClient exercises the service from its own thread; requests are
    # serialized so sharing the connection across threads is safe here.
    svc = MergeService(
        db_path, log_path=log_path, check_same_thread=False
    )
    svc.conn.executescript(
        """
        CREATE TABLE accounts(
            region  TEXT NOT NULL,
            id      INTEGER NOT NULL,
            name    TEXT NOT NULL,
            status  TEXT NOT NULL DEFAULT 'new',
            score   INTEGER,
            balance REAL NOT NULL DEFAULT 0,
            tier    TEXT,
            PRIMARY KEY (region, id)
        );
        CREATE TABLE ledger(
            book TEXT NOT NULL,
            seq  INTEGER NOT NULL,
            amt  REAL,
            note TEXT,
            PRIMARY KEY (book, seq)
        );
        CREATE TABLE no_pk_orders(
            region TEXT,
            id     INTEGER,
            status TEXT
        );
        INSERT INTO accounts(region,id,name,status,score,balance,tier) VALUES
            ('cn', 1, 'alpha', 'active', 10, 100.0, 'gold'),
            ('cn', 2, 'beta',  'active',  5, 200.0, 'silver'),
            ('cn', 3, 'gamma', 'frozen',  2, 300.0, NULL),
            ('us', 1, 'delta', 'active',  7,  50.0, 'bronze'),
            ('us', 2, 'epsil', 'churned', 1,   0.0, NULL);
        """
    )
    svc.conn.commit()
    yield svc
    svc.close()


def accounts_columns() -> list[str]:
    return ["region", "id", "name", "status", "score", "balance", "tier"]


def base_spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "target_table": "accounts",
        "key_columns": ["region", "id"],
        "update_columns": ["name", "status", "score", "balance", "tier"],
        "insert_columns": ["name", "status", "score", "balance", "tier"],
        "null_policy": "NULLS_NOT_DISTINCT",
        "when_clauses": [
            {
                "matched": True,
                "action": "update",
                "condition": "T.status = 'active' AND S.delta >= 0",
                "assignments": {
                    "name": "S.name",
                    "status": "'active'",
                    "score": "S.score",
                    "balance": "T.balance + S.delta",
                    "tier": "S.tier",
                },
            },
            {
                "matched": True,
                "action": "delete",
                "condition": "S.delta < 0",
                "assignments": {},
            },
            {
                "matched": False,
                "action": "insert",
                "condition": "S.delta >= 0",
                "assignments": {
                    "name": "S.name",
                    "status": "'active'",
                    "score": "S.score",
                    "balance": "S.delta",
                    "tier": "S.tier",
                },
            },
        ],
    }
    spec.update(overrides)
    return spec


def records_source(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"format": "records", "records": rows}


def arrow_source(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a real pyarrow.Table (covers the adapter's Arrow path)."""
    columns: dict[str, list[Any]] = {}
    for row in rows:
        for k, v in row.items():
            columns.setdefault(k, []).append(v)
    # pad missing keys per column to row count
    arrays = {}
    for k, vals in columns.items():
        if len(vals) < len(rows):
            # sparse fill with None using ordered walk
            filled = [r.get(k) for r in rows]
        else:
            filled = vals
        kind: Any = pa.string()
        if all(v is None or isinstance(v, bool) for v in filled):
            kind = pa.bool_()
        elif all(v is None or isinstance(v, int) and not isinstance(v, bool) for v in filled):
            kind = pa.int64()
        elif all(v is None or isinstance(v, (int, float)) for v in filled):
            kind = pa.float64()
        arrays[k] = pa.array(filled, type=kind)
    return {"format": "arrow", "table": pa.table(arrays)}


def make_request(spec: dict[str, Any], source: dict[str, Any], **opts: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"merge": spec, "source": source}
    if opts:
        body["options"] = opts
    return body
