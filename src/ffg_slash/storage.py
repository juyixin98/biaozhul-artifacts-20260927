"""SQLite-backed index storage.

Persists validator snapshots, accepted votes, verified evidence, per-link
voter weights (for finality rebuild), justified/finalized checkpoints,
penalty marks and an append-only event log. All SQL uses bound parameters.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import SCHEMA_VERSION
from .models import Vote

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL,
    chain_id INTEGER NOT NULL,
    genesis_root BLOB NOT NULL,
    justified_epoch INTEGER NOT NULL,
    justified_root BLOB NOT NULL,
    finalized_epoch INTEGER,
    finalized_root BLOB
);

CREATE TABLE IF NOT EXISTS epoch_weights (
    epoch INTEGER NOT NULL,
    pubkey BLOB NOT NULL,
    weight INTEGER NOT NULL CHECK (weight > 0),
    PRIMARY KEY (epoch, pubkey)
);

CREATE TABLE IF NOT EXISTS votes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id INTEGER NOT NULL,
    validator_pubkey BLOB NOT NULL,
    source_epoch INTEGER NOT NULL,
    source_root BLOB NOT NULL,
    target_epoch INTEGER NOT NULL,
    target_root BLOB NOT NULL,
    message_root BLOB NOT NULL,
    signature BLOB NOT NULL,
    seq INTEGER NOT NULL,
    UNIQUE (validator_pubkey, target_epoch, message_root)
);
CREATE INDEX IF NOT EXISTS idx_votes_validator ON votes(validator_pubkey);

CREATE TABLE IF NOT EXISTS evidences (
    evidence_id TEXT PRIMARY KEY,
    offense TEXT NOT NULL,
    validator_pubkey BLOB NOT NULL,
    packet_json TEXT NOT NULL,
    seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS link_voters (
    source_epoch INTEGER NOT NULL,
    source_root BLOB NOT NULL,
    target_epoch INTEGER NOT NULL,
    target_root BLOB NOT NULL,
    pubkey BLOB NOT NULL,
    weight INTEGER NOT NULL,
    PRIMARY KEY (source_epoch, source_root, target_epoch, target_root, pubkey)
);

CREATE TABLE IF NOT EXISTS slash_marks (
    validator_pubkey BLOB NOT NULL,
    epoch INTEGER NOT NULL,
    weight INTEGER NOT NULL,
    evidence_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (validator_pubkey, epoch)
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    validator BLOB,
    detail TEXT,
    payload TEXT
);
"""


class Storage:
    def __init__(self, path: str | Path | None = ":memory:"):
        if path == ":memory:":
            self.conn = sqlite3.connect(":memory:", check_same_thread=False)
            self.path = None
        else:
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ meta / cfg
    def init_meta(self, chain_id: int, genesis_root: bytes) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(id, schema_version, chain_id, genesis_root,"
            " justified_epoch, justified_root, finalized_epoch, finalized_root)"
            " VALUES (1, ?, ?, ?, 0, ?, NULL, NULL)",
            (SCHEMA_VERSION, chain_id, genesis_root, genesis_root),
        )
        self.conn.commit()

    def upsert_epoch(self, epoch: int, members: dict[bytes, int]) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO epoch_weights(epoch, pubkey, weight) VALUES (?, ?, ?)",
            [(epoch, pk, w) for pk, w in members.items()],
        )
        self.conn.commit()

    def load_epochs(self) -> dict[int, dict[bytes, int]]:
        out: dict[int, dict[bytes, int]] = {}
        for row in self.conn.execute("SELECT epoch, pubkey, weight FROM epoch_weights"):
            out.setdefault(row["epoch"], {})[row["pubkey"]] = row["weight"]
        return out

    # ------------------------------------------------------------------ votes
    def insert_vote(self, vote: Vote, message_root: bytes, seq: int) -> None:
        self.conn.execute(
            "INSERT INTO votes(chain_id, validator_pubkey, source_epoch, source_root,"
            " target_epoch, target_root, message_root, signature, seq)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (vote.chain_id, vote.validator_pubkey, vote.source_epoch, vote.source_root,
             vote.target_epoch, vote.target_root, message_root, vote.signature, seq),
        )

    def seen_message_roots(self, pubkey: bytes, target_epoch: int) -> set[bytes]:
        rows = self.conn.execute(
            "SELECT message_root FROM votes WHERE validator_pubkey=? AND target_epoch=?",
            (pubkey, target_epoch),
        ).fetchall()
        return {r["message_root"] for r in rows}

    def votes_by_validator(self, pubkey: bytes) -> list[Vote]:
        rows = self.conn.execute(
            "SELECT * FROM votes WHERE validator_pubkey=? ORDER BY id", (pubkey,)
        ).fetchall()
        return [self._row_to_vote(r) for r in rows]

    @staticmethod
    def _row_to_vote(r: sqlite3.Row) -> Vote:
        return Vote(
            chain_id=r["chain_id"],
            validator_pubkey=r["validator_pubkey"],
            source_epoch=r["source_epoch"],
            source_root=r["source_root"],
            target_epoch=r["target_epoch"],
            target_root=r["target_root"],
            signature=r["signature"],
        )

    # -------------------------------------------------------------- evidences
    def insert_evidence(self, evidence_id: str, offense: str,
                        pubkey: bytes, packet: dict, seq: int) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO evidences(evidence_id, offense, validator_pubkey,"
            " packet_json, seq) VALUES (?,?,?,?,?)",
            (evidence_id, offense, pubkey, json.dumps(packet, sort_keys=True), seq),
        )
        return cur.rowcount > 0

    def list_evidences(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT packet_json FROM evidences ORDER BY seq").fetchall()
        return [json.loads(r["packet_json"]) for r in rows]

    def get_evidence(self, evidence_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT packet_json FROM evidences WHERE evidence_id=?", (evidence_id,)
        ).fetchone()
        return json.loads(row["packet_json"]) if row else None

    # ------------------------------------------------------------ link voters
    def add_link_voter(self, key: tuple, pubkey: bytes, weight: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO link_voters(source_epoch, source_root, target_epoch,"
            " target_root, pubkey, weight) VALUES (?,?,?,?,?,?)",
            (key[0], key[1], key[2], key[3], pubkey, weight),
        )

    def load_link_voters(self) -> list[tuple]:
        return [tuple(r) for r in self.conn.execute(
            "SELECT source_epoch, source_root, target_epoch, target_root, pubkey, weight"
            " FROM link_voters")]

    # ------------------------------------------------------------ slash marks
    def add_slash_mark(self, pubkey: bytes, epoch: int, weight: int,
                       evidence_id: str, seq: int) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO slash_marks(validator_pubkey, epoch, weight,"
            " evidence_id, seq) VALUES (?,?,?,?,?)",
            (pubkey, epoch, weight, evidence_id, seq),
        )
        return cur.rowcount > 0

    def slash_marks(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT validator_pubkey, epoch, weight, evidence_id, seq"
            " FROM slash_marks ORDER BY seq, epoch").fetchall()

    # ------------------------------------------------------------- checkpoints
    def save_checkpoints(self, justified: tuple[int, bytes],
                         finalized: tuple[int, bytes] | None) -> None:
        self.conn.execute(
            "UPDATE meta SET justified_epoch=?, justified_root=?,"
            " finalized_epoch=?, finalized_root=? WHERE id=1",
            (justified[0], justified[1],
             finalized[0] if finalized else None, finalized[1] if finalized else None),
        )

    def load_checkpoints(self) -> tuple[tuple[int, bytes], tuple[int, bytes] | None]:
        row = self.conn.execute(
            "SELECT justified_epoch, justified_root, finalized_epoch, finalized_root"
            " FROM meta WHERE id=1").fetchone()
        fin = (row["finalized_epoch"], row["finalized_root"]) if row["finalized_epoch"] is not None else None
        return (row["justified_epoch"], row["justified_root"]), fin

    # ------------------------------------------------------------------ events
    def append_event(self, run_id: str, kind: str,
                     validator: bytes | None, detail: str,
                     payload: dict | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO events(run_id, kind, validator, detail, payload)"
            " VALUES (?,?,?,?,?)",
            (run_id, kind, validator, detail,
             json.dumps(payload, sort_keys=True, default=str) if payload else None),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def events(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM events ORDER BY seq").fetchall()

    def commit(self) -> None:
        self.conn.commit()
