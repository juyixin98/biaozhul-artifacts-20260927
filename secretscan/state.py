"""Per-workspace SQLite state, fully isolated from other workspaces.

Each workspace has its own database file (the directory containing it is
created if missing). Tables store **masks and keyed fingerprints only** — raw
secret values never touch disk.

Lifecycle model (a finding is a ``fingerprint + rule_id`` pair):

* ``new``          first scan that sees it
* ``open``         seen in the latest scan after having been ``new``
* ``moved``        latest scan still has the content, but at a different path
* ``known_fixed``  the content was absent from a scan and the old file still
                   exists with different contents (evidence of remediation;
                   a classification, not a verified claim — the secret was
                   never confirmed live and never validated over a network)
* ``uncertain_removal`` the content was absent but the old path also vanished
                   (file deleted/renamed/ignored — can't distinguish removal
                   from relocation; listed under uncertainties)
* ``baseline_exempt`` content is on the reviewed baseline (this scan)

Every transition is written to ``audit_events`` in the same transaction as the
state change, so the audit trail cannot diverge from the stored state.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    root TEXT NOT NULL,
    rule_pack_version TEXT NOT NULL,
    rule_pack_fingerprint TEXT NOT NULL,
    scope_pack_version TEXT NOT NULL,
    scope_pack_fingerprint TEXT NOT NULL,
    pepper_id TEXT NOT NULL,
    request_id TEXT,
    actor_id TEXT,
    baseline_path TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    summary_json TEXT
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    mask TEXT NOT NULL,
    first_scan_id INTEGER NOT NULL REFERENCES scans(id),
    latest_scan_id INTEGER NOT NULL REFERENCES scans(id),
    first_seen_at TEXT NOT NULL,
    latest_seen_at TEXT NOT NULL,
    state TEXT NOT NULL,
    confidence TEXT NOT NULL,
    UNIQUE(fingerprint, rule_id)
);
CREATE TABLE IF NOT EXISTS occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    finding_id INTEGER NOT NULL REFERENCES findings(id),
    relpath TEXT NOT NULL,
    line INTEGER,
    column INTEGER NOT NULL,
    end_line INTEGER,
    end_column INTEGER NOT NULL,
    evidence_masked TEXT NOT NULL,
    entropy REAL NOT NULL,
    content_media TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    exempt INTEGER NOT NULL DEFAULT 0,
    UNIQUE(scan_id, finding_id, relpath, line, column, end_line, end_column)
);
CREATE TABLE IF NOT EXISTS file_inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    relpath TEXT NOT NULL,
    size INTEGER NOT NULL,
    status TEXT NOT NULL,
    media TEXT,
    reason TEXT,
    sha256 TEXT,
    UNIQUE(scan_id, relpath)
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER REFERENCES scans(id),
    ts TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    request_id TEXT,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target TEXT NOT NULL,
    details_json TEXT NOT NULL,
    outcome TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_occ_scan ON occurrences(scan_id);
CREATE INDEX IF NOT EXISTS idx_findings_latest ON findings(latest_scan_id);
CREATE INDEX IF NOT EXISTS idx_inv_scan ON file_inventory(scan_id);
CREATE INDEX IF NOT EXISTS idx_audit_scan ON audit_events(scan_id);
"""


class StateError(RuntimeError):
    """A persistence-level consistency problem."""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (and if needed create) an isolated workspace database.

    ``check_same_thread`` is disabled because the FastAPI app runs handlers on
    worker threads while the connection is created at app startup; the tool is
    local and single-writer (scan transactions are serialised by SQLite).
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    existing = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),))
        conn.commit()
    elif int(existing["value"]) != SCHEMA_VERSION:
        raise StateError(
            f"database schema {existing['value']} != supported {SCHEMA_VERSION}")
    return conn


@dataclass(frozen=True)
class PreviousOccurrence:
    finding_id: int
    fingerprint: str
    rule_id: str
    mask: str
    relpath: str
    file_sha256: str
    line: int | None
    column: int
    end_line: int | None
    end_column: int


def latest_scan_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT MAX(id) AS id FROM scans WHERE status='completed'").fetchone()
    return row["id"] if row and row["id"] is not None else None


def latest_scan_id_excluding(conn: sqlite3.Connection,
                             scan_id: int) -> int | None:
    """Most recent completed scan other than ``scan_id`` (the diff baseline)."""
    row = conn.execute(
        "SELECT MAX(id) AS id FROM scans WHERE status='completed' AND id<>?",
        (scan_id,)).fetchone()
    return row["id"] if row and row["id"] is not None else None


def previous_occurrences(conn: sqlite3.Connection,
                         scan_id: int) -> list[PreviousOccurrence]:
    """Occurrences recorded by the most recent earlier completed scan."""
    rows = conn.execute(
        """
        SELECT f.id AS finding_id, f.fingerprint, f.rule_id, f.mask,
               o.relpath, o.file_sha256, o.line, o.column, o.end_line,
               o.end_column
        FROM occurrences o
        JOIN findings f ON f.id = o.finding_id
        WHERE o.scan_id = ? AND COALESCE(o.exempt, 0) = 0
        """, (scan_id,)).fetchall()
    return [PreviousOccurrence(**dict(r)) for r in rows]


def current_file_index(conn: sqlite3.Connection,
                       scan_id: int) -> dict[str, sqlite3.Row]:
    """File inventory rows inserted for the current (in-progress) scan."""
    rows = conn.execute(
        "SELECT * FROM file_inventory WHERE scan_id = ?", (scan_id,)).fetchall()
    return {r["relpath"]: r for r in rows}


def insert_scan(conn: sqlite3.Connection, *, started_at: str, root: str,
                rule_version: str, rule_fingerprint: str,
                scope_version: str, scope_fingerprint: str, pepper_id: str,
                request_id: str | None, actor_id: str,
                baseline_path: str | None) -> int:
    cur = conn.execute(
        """
        INSERT INTO scans(started_at, root, rule_pack_version,
            rule_pack_fingerprint, scope_pack_version, scope_pack_fingerprint,
            pepper_id, request_id, actor_id, baseline_path)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (started_at, root, rule_version, rule_fingerprint, scope_version,
         scope_fingerprint, pepper_id, request_id, actor_id, baseline_path))
    return int(cur.lastrowid)


def insert_inventory(conn: sqlite3.Connection, scan_id: int,
                     inventory: Iterable[Any]) -> None:
    conn.executemany(
        """
        INSERT INTO file_inventory(scan_id, relpath, size, status, media,
            reason, sha256)
        VALUES (?,?,?,?,?,?,?)
        """,
        [(scan_id, f.relpath, f.size, f.status, f.media, f.reason, f.sha256)
         for f in inventory])


def upsert_finding(conn: sqlite3.Connection, *, fingerprint: str, rule_id: str,
                   mask: str, scan_id: int, seen_at: str, state: str,
                   confidence: str) -> int:
    """Create or advance a finding; returns its id."""
    row = conn.execute(
        "SELECT id, first_scan_id, first_seen_at FROM findings "
        "WHERE fingerprint=? AND rule_id=?",
        (fingerprint, rule_id)).fetchone()
    if row is None:
        cur = conn.execute(
            """
            INSERT INTO findings(fingerprint, rule_id, mask, first_scan_id,
                latest_scan_id, first_seen_at, latest_seen_at, state,
                confidence)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (fingerprint, rule_id, mask, scan_id, scan_id, seen_at, seen_at,
             state, confidence))
        return int(cur.lastrowid)
    conn.execute(
        """
        UPDATE findings SET latest_scan_id=?, latest_seen_at=?, state=?,
            mask=?, confidence=?
        WHERE id=?
        """,
        (scan_id, seen_at, state, mask, confidence, row["id"]))
    return int(row["id"])


def insert_occurrence(conn: sqlite3.Connection, *, scan_id: int,
                      finding_id: int, candidate: Any, exempt: bool) -> None:
    conn.execute(
        """
        INSERT INTO occurrences(scan_id, finding_id, relpath, line, column,
            end_line, end_column, evidence_masked, entropy, content_media,
            file_sha256, file_size, exempt)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (scan_id, finding_id, candidate.relpath, candidate.line,
         candidate.column, candidate.end_line, candidate.end_column,
         candidate.evidence_masked, candidate.entropy,
         candidate.content_media, candidate.file_sha256, candidate.file_size,
         1 if exempt else 0))


def mark_finding_state(conn: sqlite3.Connection, finding_id: int,
                       state: str, scan_id: int, seen_at: str) -> None:
    conn.execute(
        "UPDATE findings SET state=?, latest_scan_id=?, latest_seen_at=? "
        "WHERE id=?",
        (state, scan_id, seen_at, finding_id))


def insert_audit(conn: sqlite3.Connection, *, scan_id: int | None, ts: str,
                 actor_id: str, request_id: str | None, action: str,
                 target_type: str, target: str, details: dict,
                 outcome: str) -> None:
    conn.execute(
        """
        INSERT INTO audit_events(scan_id, ts, actor_id, request_id, action,
            target_type, target, details_json, outcome)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (scan_id, ts, actor_id, request_id, action, target_type, target,
         json.dumps(details, ensure_ascii=False, sort_keys=True), outcome))


def complete_scan(conn: sqlite3.Connection, scan_id: int, finished_at: str,
                  summary: dict) -> None:
    conn.execute(
        "UPDATE scans SET status='completed', finished_at=?, summary_json=? "
        "WHERE id=?",
        (finished_at,
         json.dumps(summary, ensure_ascii=False, sort_keys=True),
         scan_id))
