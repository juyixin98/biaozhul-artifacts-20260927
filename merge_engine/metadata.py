"""Metadata transaction boundary.

All run bookkeeping lives in four tables inside the same SQLite database file
as the target data, but **never** in the same write transaction as the data
changes:

* merge_runs       - one row per merge invocation (planned / committed /
                     rejected / failed), with fingerprints, spec and summary
* merge_actions    - every planned action per run (also persisted for rejected
                     runs so the action set is replayable)
* merge_traces     - per-source-row decision trace with clause results/reason
* merge_snapshots  - source + target payloads at plan time (full replay input)

A run is therefore replayable from its run id alone: spec, inputs, target
snapshot and the decided action set are all queryable locally.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from .contract import MergePlan, as_public_dict
from .errors import (
    COMMIT_FAILED_CODE,
    ResourceExhaustedError,
)
from .utils import canonical_json, quote_ident, short_hash

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS merge_meta_version(
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS merge_runs(
    run_id              TEXT PRIMARY KEY,
    seq                 INTEGER NOT NULL,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL,  -- planned|committed|rejected|failed
    target_table        TEXT NOT NULL,
    null_policy         TEXT NOT NULL,
    spec_json           TEXT NOT NULL,
    source_fingerprint  TEXT NOT NULL,
    target_fingerprint  TEXT NOT NULL,
    source_rows         INTEGER NOT NULL,
    target_rows         INTEGER NOT NULL,
    n_update            INTEGER NOT NULL DEFAULT 0,
    n_insert            INTEGER NOT NULL DEFAULT 0,
    n_delete            INTEGER NOT NULL DEFAULT 0,
    n_unprocessed       INTEGER NOT NULL DEFAULT 0,
    error_category      TEXT,
    error_code          TEXT,
    error_message       TEXT,
    error_details_json  TEXT
);

CREATE TABLE IF NOT EXISTS merge_actions(
    run_id          TEXT NOT NULL REFERENCES merge_runs(run_id),
    seq             INTEGER NOT NULL,
    outcome         TEXT NOT NULL,
    source_index    INTEGER NOT NULL,
    target_rowid    INTEGER,
    key_json        TEXT NOT NULL,
    new_values_json TEXT NOT NULL,
    PRIMARY KEY(run_id, seq)
);

CREATE TABLE IF NOT EXISTS merge_traces(
    run_id          TEXT NOT NULL REFERENCES merge_runs(run_id),
    source_index    INTEGER NOT NULL,
    matched         INTEGER NOT NULL,
    target_rowid    INTEGER,
    outcome         TEXT NOT NULL,
    fired_clause    INTEGER,
    reason          TEXT NOT NULL,
    key_json        TEXT NOT NULL,
    clause_results_json TEXT NOT NULL,
    PRIMARY KEY(run_id, source_index)
);

CREATE TABLE IF NOT EXISTS merge_snapshots(
    run_id          TEXT PRIMARY KEY REFERENCES merge_runs(run_id),
    source_json     TEXT NOT NULL,
    target_json     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merge_run_seq(
    id INTEGER PRIMARY KEY CHECK(id = 1),
    next_seq INTEGER NOT NULL
);
"""


def init_metadata(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    cur = conn.execute("SELECT schema_version FROM merge_meta_version")
    if cur.fetchone() is None:
        conn.execute(
            "INSERT INTO merge_meta_version(schema_version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
    conn.execute(
        "INSERT OR IGNORE INTO merge_run_seq(id, next_seq) VALUES (1, 1)"
    )
    conn.commit()


# --------------------------------------------------------------------- ids
def new_run_id(conn: sqlite3.Connection) -> tuple[str, int]:
    """Monotonic per-DB seq + time/suffix run id.

    Format: ``MR-YYYYMMDDTHHMMSS-microsec-<6hex>`` plus the integer seq.
    The seq guarantees uniqueness even inside one timestamp; the suffix keeps
    ids readable across separately-created database copies.
    """
    try:
        cur = conn.execute("SELECT next_seq FROM merge_run_seq WHERE id = 1")
        seq = int(cur.fetchone()[0])
        conn.execute("UPDATE merge_run_seq SET next_seq = ? WHERE id = 1", (seq + 1,))
        conn.commit()
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            from .errors import DB_LOCKED_CODE, ResourceExhaustedError

            raise ResourceExhaustedError(
                DB_LOCKED_CODE,
                f"could not allocate run id: database is locked: {exc}",
            )
        raise
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%f")
    return f"MR-{stamp}-{short_hash()}", seq


# ----------------------------------------------------------------- writing
def insert_early_rejected_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    seq: int,
    started_at: str,
    target_table: str,
    null_policy: str,
    raw_spec: Any,
    error: Any,
) -> None:
    """Record a run rejected before a plan could be built (bad spec/source)."""
    conn.execute(
        """
        INSERT INTO merge_runs(
            run_id, seq, started_at, finished_at, status, target_table,
            null_policy, spec_json, source_fingerprint, target_fingerprint,
            source_rows, target_rows, error_category, error_code,
            error_message, error_details_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            seq,
            started_at,
            datetime.now(timezone.utc).isoformat(),
            "rejected",
            target_table,
            null_policy,
            canonical_json(raw_spec),
            "",
            "",
            0,
            0,
            getattr(getattr(error, "category", None), "value", None),
            getattr(error, "code", None),
            str(error),
            canonical_json(getattr(error, "details", {}) or {}),
        ),
    )


def insert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    seq: int,
    plan: MergePlan,
    started_at: str,
) -> None:
    spec = plan.spec
    counts = plan.counts()
    conn.execute(
        """
        INSERT INTO merge_runs(
            run_id, seq, started_at, status, target_table, null_policy,
            spec_json, source_fingerprint, target_fingerprint,
            source_rows, target_rows, n_update, n_insert, n_delete, n_unprocessed
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            seq,
            started_at,
            "planned",
            spec.target_table,
            spec.null_policy.value,
            canonical_json(as_public_dict(spec)),
            plan.source_fingerprint,
            plan.target_fingerprint,
            plan.source_count,
            plan.target_count,
            counts["update"],
            counts["insert"],
            counts["delete"],
            counts["unprocessed"],
        ),
    )


def insert_plan_payload(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    plan: MergePlan,
    source_payload: list[dict[str, Any]],
    target_payload: list[dict[str, Any]],
) -> None:
    conn.executemany(
        """
        INSERT INTO merge_actions(
            run_id, seq, outcome, source_index, target_rowid,
            key_json, new_values_json
        ) VALUES (?,?,?,?,?,?,?)
        """,
        [
            (
                run_id,
                a.seq,
                a.outcome.value,
                a.source_index,
                a.target_rowid,
                canonical_json(list(a.key)),
                canonical_json(a.new_values),
            )
            for a in plan.actions
        ],
    )
    conn.executemany(
        """
        INSERT INTO merge_traces(
            run_id, source_index, matched, target_rowid, outcome,
            fired_clause, reason, key_json, clause_results_json
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                run_id,
                d.source_index,
                1 if d.matched else 0,
                d.target_rowid,
                d.outcome.value,
                d.fired_clause,
                d.reason,
                canonical_json(list(d.key)),
                canonical_json(as_public_dict(d.clause_results)),
            )
            for d in plan.decisions
        ],
    )
    conn.execute(
        "INSERT INTO merge_snapshots(run_id, source_json, target_json) VALUES (?,?,?)",
        (run_id, canonical_json(source_payload), canonical_json(target_payload)),
    )


def finish_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    error: Any = None,
) -> None:
    finished = datetime.now(timezone.utc).isoformat()
    if error is None:
        conn.execute(
            "UPDATE merge_runs SET status = ?, finished_at = ?, "
            "error_category = NULL, error_code = NULL, error_message = NULL, "
            "error_details_json = NULL WHERE run_id = ?",
            (status, finished, run_id),
        )
        return

    category = getattr(getattr(error, "category", None), "value", None)
    conn.execute(
        """
        UPDATE merge_runs
        SET status = ?, finished_at = ?, error_category = ?, error_code = ?,
            error_message = ?, error_details_json = ?
        WHERE run_id = ?
        """,
        (
            status,
            finished,
            category,
            getattr(error, "code", None),
            str(error),
            canonical_json(getattr(error, "details", {}) or {}),
            run_id,
        ),
    )


# ----------------------------------------------------------------- reading
def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM merge_runs WHERE run_id = ?", (run_id,))
    row = cur.fetchone()
    if row is None:
        return None
    record = {c[0]: row[i] for i, c in enumerate(cur.description)}
    record["spec"] = json.loads(record.pop("spec_json"))
    record["error_details"] = (
        json.loads(record.pop("error_details_json"))
        if record.get("error_details_json")
        else None
    )
    return record


def list_runs(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT run_id, seq, started_at, finished_at, status, target_table, "
        "null_policy, source_rows, target_rows, n_update, n_insert, n_delete, "
        "n_unprocessed, error_category, error_code "
        "FROM merge_runs ORDER BY seq DESC LIMIT ?",
        (limit,),
    )
    return [{c[0]: r[i] for i, c in enumerate(cur.description)} for r in cur.fetchall()]


def get_actions(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT seq, outcome, source_index, target_rowid, key_json, new_values_json "
        "FROM merge_actions WHERE run_id = ? ORDER BY seq",
        (run_id,),
    )
    out = []
    for r in cur.fetchall():
        out.append(
            {
                "seq": r[0],
                "outcome": r[1],
                "source_index": r[2],
                "target_rowid": r[3],
                "key": json.loads(r[4]),
                "new_values": json.loads(r[5]),
            }
        )
    return out


def get_traces(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT source_index, matched, target_rowid, outcome, fired_clause, "
        "reason, key_json, clause_results_json FROM merge_traces "
        "WHERE run_id = ? ORDER BY source_index",
        (run_id,),
    )
    out = []
    for r in cur.fetchall():
        out.append(
            {
                "source_index": r[0],
                "matched": bool(r[1]),
                "target_rowid": r[2],
                "outcome": r[3],
                "fired_clause": r[4],
                "reason": r[5],
                "key": json.loads(r[6]),
                "clause_results": json.loads(r[7]),
            }
        )
    return out


def get_snapshots(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    cur = conn.execute(
        "SELECT source_json, target_json FROM merge_snapshots WHERE run_id = ?",
        (run_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {"source": json.loads(row[0]), "target": json.loads(row[1])}


def injected_commit_failure(message: str, details: dict[str, Any]) -> ResourceExhaustedError:
    """Test knob helper: a commit failure is RESOURCE_EXHAUSTED so callers can
    distinguish it from state conflicts and computation failures."""
    return ResourceExhaustedError(COMMIT_FAILED_CODE, message, details=details)
