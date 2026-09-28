"""SQLite indexed storage.

Two groups of data are persisted:

  * registry state (validators, pubkeys, per-epoch weight updates) and chain
    meta — the trusted snapshots weights are read from;
  * an append-only ingest journal of EVERY submission (accepted, duplicate,
    offense and invalid alike) with the raw signed envelope, the exact
    classification and the run id, plus an evidence table keyed by the
    canonical evidence id.

The journal makes offline replay possible: a fresh kernel re-ingests the
exact same envelopes in seq order and must reproduce the exact same statuses,
counts and evidence set.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .epochs import ValidatorRecord, ValidatorRegistry
from .kernel import IngestResult
from .models import Evidence, SignedVote

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS validators (
    validator_id TEXT PRIMARY KEY,
    pubkey TEXT NOT NULL,
    created_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weight_updates (
    validator_id TEXT NOT NULL,
    effective_epoch INTEGER NOT NULL,
    weight INTEGER NOT NULL,
    PRIMARY KEY (validator_id, effective_epoch),
    FOREIGN KEY (validator_id) REFERENCES validators(validator_id)
);

CREATE TABLE IF NOT EXISTS ingest_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ingested_utc TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    chain_id TEXT,
    validator_id TEXT,
    source_round INTEGER,
    target_round INTEGER,
    block_root TEXT,
    signed_json TEXT NOT NULL,
    evidence_ids TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_events_validator ON ingest_events(validator_id);
CREATE INDEX IF NOT EXISTS idx_events_status ON ingest_events(status);
CREATE INDEX IF NOT EXISTS idx_events_target ON ingest_events(target_round);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    chain_id TEXT NOT NULL,
    validator_id TEXT NOT NULL,
    weight_epoch INTEGER NOT NULL,
    weight INTEGER NOT NULL,
    created_seq INTEGER,
    created_utc TEXT NOT NULL,
    bundle_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_validator ON evidence(validator_id);
CREATE INDEX IF NOT EXISTS idx_evidence_kind ON evidence(kind);
"""


class StorageError(RuntimeError):
    pass


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class VoteStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        # check_same_thread=False: FastAPI executes sync handlers on a worker
        # thread. All access is serialized by self._lock.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.commit()
            self._conn.close()
            self._closed = True

    def __enter__(self) -> "VoteStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- meta ------------------------------------------------------------- #

    def init_meta(self, *, chain_id: str, epoch_length: int, domain: bytes, allow_reinit: bool = False) -> None:
        with self._lock:
            existing = self.get_meta("chain_id")
            if existing is not None and not allow_reinit:
                if existing != chain_id:
                    raise StorageError(
                        f"db {self.db_path} already bound to chain {existing!r}, refusing {chain_id!r}"
                    )
                return
            self.set_meta("schema_version", SCHEMA_VERSION)
            self.set_meta("chain_id", chain_id)
            self.set_meta("epoch_length", str(epoch_length))
            self.set_meta("domain", domain.hex())
            self.set_meta("created_utc", _utcnow())

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row["value"]

    def load_domain(self) -> bytes:
        v = self.get_meta("domain")
        if v is None:
            raise StorageError("domain missing from meta")
        return bytes.fromhex(v)

    # -- registry --------------------------------------------------------- #

    def save_registry(self, registry: ValidatorRegistry) -> None:
        with self._lock:
            now = _utcnow()
            for vid, rec in sorted(registry._validators.items()):  # noqa: SLF001 (serialization boundary)
                self._conn.execute(
                    "INSERT INTO validators(validator_id, pubkey, created_utc) VALUES(?,?,?) "
                    "ON CONFLICT(validator_id) DO UPDATE SET pubkey=excluded.pubkey",
                    (vid, rec.pubkey.hex(), now),
                )
                for epoch, weight in rec.weight_updates:
                    self._conn.execute(
                        "INSERT INTO weight_updates(validator_id, effective_epoch, weight) VALUES(?,?,?) "
                        "ON CONFLICT(validator_id, effective_epoch) DO UPDATE SET weight=excluded.weight",
                        (vid, epoch, weight),
                    )
            self._conn.commit()

    def load_registry(self) -> ValidatorRegistry:
        chain_id = self.get_meta("chain_id")
        epoch_length = self.get_meta("epoch_length")
        if chain_id is None or epoch_length is None:
            raise StorageError("registry meta missing; init_meta() first")
        reg = ValidatorRegistry(chain_id=chain_id, epoch_length=int(epoch_length))
        rows = self._conn.execute(
            "SELECT v.validator_id AS vid, v.pubkey AS pubkey, "
            "       w.effective_epoch AS ep, w.weight AS weight "
            "FROM validators v LEFT JOIN weight_updates w "
            "ON v.validator_id = w.validator_id ORDER BY v.validator_id, w.effective_epoch"
        ).fetchall()
        staged: dict[str, dict] = {}
        for r in rows:
            entry = staged.setdefault(r["vid"], {"pubkey": bytes.fromhex(r["pubkey"]), "updates": []})
            if r["ep"] is not None:
                entry["updates"].append((int(r["ep"]), int(r["weight"])))
        for vid, entry in staged.items():
            reg._validators[vid] = ValidatorRecord(vid, entry["pubkey"], tuple(entry["updates"]))
        return reg

    # -- journal ---------------------------------------------------------- #

    def record_ingest(self, *, run_id: str, signed: SignedVote, result: IngestResult) -> int:
        with self._lock:
            v = signed.vote
            evidence_ids = json.dumps([e.evidence_id for e in result.evidence], sort_keys=True)
            cur = self._conn.execute(
                """
                INSERT INTO ingest_events(
                    run_id, ingested_utc, status, reason, chain_id, validator_id,
                    source_round, target_round, block_root, signed_json, evidence_ids)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    _utcnow(),
                    result.status.value,
                    result.reason,
                    v.chain_id,
                    v.validator_id,
                    v.source_round,
                    v.target_round,
                    v.block_root.hex(),
                    json.dumps(signed.to_json_dict(), sort_keys=True),
                    evidence_ids,
                ),
            )
            seq = int(cur.lastrowid)
            for ev in result.evidence:
                self._upsert_evidence(ev, created_seq=seq)
            self._conn.commit()
            return seq

    def _upsert_evidence(self, ev: Evidence, *, created_seq: int) -> None:
        self._conn.execute(
            """
            INSERT INTO evidence(evidence_id, kind, chain_id, validator_id, weight_epoch,
                                 weight, created_seq, created_utc, bundle_json)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(evidence_id) DO NOTHING
            """,
            (
                ev.evidence_id,
                ev.kind.value,
                ev.chain_id,
                ev.validator_id,
                ev.weight_epoch,
                ev.weight,
                created_seq,
                _utcnow(),
                json.dumps(ev.to_json_dict(), sort_keys=True),
            ),
        )

    def iter_events(self) -> Iterator[dict]:
        for row in self._conn.execute(
            "SELECT seq, run_id, ingested_utc, status, reason, signed_json, evidence_ids "
            "FROM ingest_events ORDER BY seq ASC"
        ):
            yield {
                "seq": row["seq"],
                "run_id": row["run_id"],
                "ingested_utc": row["ingested_utc"],
                "status": row["status"],
                "reason": row["reason"],
                "signed_json": json.loads(row["signed_json"]),
                "evidence_ids": json.loads(row["evidence_ids"]),
            }

    def status_counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM ingest_events GROUP BY status").fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    # -- evidence --------------------------------------------------------- #

    def list_evidence(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT bundle_json FROM evidence ORDER BY created_seq ASC, evidence_id ASC"
        ).fetchall()
        return [json.loads(r["bundle_json"]) for r in rows]

    def get_evidence(self, evidence_id_: str) -> dict | None:
        row = self._conn.execute(
            "SELECT bundle_json FROM evidence WHERE evidence_id=?", (evidence_id_,)
        ).fetchone()
        return None if row is None else json.loads(row["bundle_json"])

    def evidence_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS n FROM evidence").fetchone()["n"])
