"""Persistent state with hard isolation.

Layout under a state directory:

  state_dir/
    registry.db                       # project root <-> project_id mapping
    projects/<project_id>/project.db  # scans, candidates, baseline, audit

A scan of project A can never see project B's candidates or audit trail:
they live in separate SQLite files. Candidate HMAC keys (master_key, salt)
are also per-project and never leave the project database.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from .errors import StorageError
from .fingerprint import MASTER_LENGTH, SALT_LENGTH
from .idutils import project_id_for, utcnow_iso

REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id     TEXT PRIMARY KEY,
    root           TEXT NOT NULL UNIQUE,
    created_at     TEXT NOT NULL,
    rules_version  TEXT NOT NULL DEFAULT '',
    note           TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    request_id  TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    project_id  TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_registry_audit_project ON audit_events(project_id, event_id);
"""

PROJECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scans (
    scan_id                TEXT PRIMARY KEY,
    project_id             TEXT NOT NULL,
    root                   TEXT NOT NULL,
    rules_version          TEXT NOT NULL,
    classification_version TEXT NOT NULL,
    config_digest          TEXT NOT NULL,
    request_id             TEXT NOT NULL,
    started_at             TEXT NOT NULL,
    finished_at            TEXT NOT NULL,
    result_json            TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id             TEXT NOT NULL,
    fingerprint         TEXT NOT NULL,
    masked              TEXT NOT NULL,
    category            TEXT NOT NULL,
    confidence          TEXT NOT NULL,
    entropy             REAL NOT NULL,
    source              TEXT NOT NULL,
    uncertain           INTEGER NOT NULL,
    reasons_json        TEXT NOT NULL,
    state               TEXT NOT NULL,
    triage              TEXT NOT NULL,
    first_seen_scan_id  TEXT NOT NULL,
    last_seen_scan_id   TEXT NOT NULL,
    state_updated_scan  TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE(rule_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS baseline_exemptions (
    rule_id          TEXT NOT NULL,
    fingerprint      TEXT NOT NULL,
    masked           TEXT NOT NULL,
    accepted_scan_id TEXT NOT NULL,
    request_id       TEXT NOT NULL,
    actor            TEXT NOT NULL,
    accepted_at      TEXT NOT NULL,
    note             TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (rule_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    request_id  TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_scans_time ON scans(started_at);
CREATE INDEX IF NOT EXISTS idx_candidates_state ON candidates(state);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(event_id);
"""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the HTTP audit interface (ASGI TestClient /
    # uvicorn worker) may touch a connection from its portal thread. Access is
    # serialized by Store._lock, so cross-thread sharing stays safe.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


class Store:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.state_dir / "registry.db"
        self._lock = threading.RLock()
        self._registry = _connect(self.registry_path)
        self._registry.executescript(REGISTRY_SCHEMA)
        self._registry.commit()
        self._project_conns: dict[str, sqlite3.Connection] = {}

    def close(self) -> None:
        for conn in self._project_conns.values():
            conn.close()
        self._registry.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------- registry ops
    def project_db_path(self, project_id: str) -> Path:
        return self.state_dir / "projects" / project_id / "project.db"

    def project_conn(self, project_id: str) -> sqlite3.Connection:
        conn = self._project_conns.get(project_id)
        if conn is None:
            conn = _connect(self.project_db_path(project_id))
            conn.executescript(PROJECT_SCHEMA)
            conn.commit()
            self._project_conns[project_id] = conn
        return conn

    def get_or_register_project(
        self,
        root: str,
        *,
        rules_version: str,
        note: str = "",
    ) -> tuple[str, bytes, bytes, bool]:
        """Return (project_id, master_key, salt, created)."""
        project_id = project_id_for(root)
        conn = self.project_conn(project_id)
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
        meta = {r["key"]: r["value"] for r in rows}
        created = False
        if "master_key" not in meta or "salt" not in meta:
            master_key = os.urandom(MASTER_LENGTH)
            salt = os.urandom(SALT_LENGTH)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('master_key', ?)",
                (master_key.hex(),),
            )
            conn.execute("INSERT INTO meta(key, value) VALUES('salt', ?)", (salt.hex(),))
            conn.commit()
            created = True
        else:
            master_key = bytes.fromhex(meta["master_key"])
            salt = bytes.fromhex(meta["salt"])

        # Register in the global registry (idempotent).
        existing = self._registry.execute(
            "SELECT project_id FROM projects WHERE root = ?", (root,)
        ).fetchone()
        if existing is None:
            self._registry.execute(
                "INSERT INTO projects(project_id, root, created_at, rules_version, note) "
                "VALUES(?, ?, ?, ?, ?)",
                (project_id, root, utcnow_iso(), rules_version, note),
            )
            self._registry.commit()
        return project_id, master_key, salt, created

    def list_projects(self) -> list[dict]:
        rows = self._registry.execute(
            "SELECT project_id, root, created_at, rules_version, note FROM projects ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_project(self, project_id: str) -> dict | None:
        row = self._registry.execute(
            "SELECT project_id, root, created_at, rules_version, note FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return dict(row) if row else None

    def project_keys(self, project_id: str) -> tuple[bytes, bytes]:
        conn = self.project_conn(project_id)
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
        meta = {r["key"]: r["value"] for r in rows}
        try:
            return bytes.fromhex(meta["master_key"]), bytes.fromhex(meta["salt"])
        except KeyError as exc:
            raise StorageError(f"project {project_id} is missing key material") from exc

    # ------------------------------------------------------------- audit ops
    def append_registry_audit(
        self, *, request_id: str, actor: str, action: str, project_id: str, detail: dict
    ) -> None:
        self._registry.execute(
            "INSERT INTO audit_events(at, request_id, actor, action, project_id, detail_json) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (utcnow_iso(), request_id, actor, action, project_id, json.dumps(detail, sort_keys=True)),
        )
        self._registry.commit()

    def append_project_audit(
        self, project_id: str, *, request_id: str, actor: str, action: str, detail: dict
    ) -> None:
        conn = self.project_conn(project_id)
        conn.execute(
            "INSERT INTO audit_events(at, request_id, actor, action, detail_json) "
            "VALUES(?, ?, ?, ?, ?)",
            (utcnow_iso(), request_id, actor, action, json.dumps(detail, sort_keys=True)),
        )
        conn.commit()

    def list_project_audit(self, project_id: str, limit: int = 100) -> list[dict]:
        conn = self.project_conn(project_id)
        rows = conn.execute(
            "SELECT event_id, at, request_id, actor, action, detail_json "
            "FROM audit_events ORDER BY event_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out

    def list_registry_audit(self, limit: int = 100) -> list[dict]:
        rows = self._registry.execute(
            "SELECT event_id, at, request_id, actor, action, project_id, detail_json "
            "FROM audit_events ORDER BY event_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out
