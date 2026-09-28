"""Index storage: SQLite-backed, transactional, queryable header store.

Schema contract
---------------
* ``headers``    - every accepted header keyed by its 32-byte root, indexed by
                   round and parent (drives child/branch lookups).
* ``committees`` - every committee seen at the point a rotation lands, keyed
                   by its 32-byte commitment; stores canonical encoding + JSON.
* ``meta``       - single-row trusted tip pointer: ``tip_header_root`` plus a
                   monotonic sequence.

Atomicity
---------
``chain_txn()`` opens ``BEGIN IMMEDIATE``; all kernel writes for one header or
one replay batch happen inside one transaction. On any exception the
transaction is rolled back, so a rejected update leaves the trusted tip
byte-identical (the tests assert this via before/after snapshots).

Resource limits are enforced in Python (``resource_*`` config); oversized
input is rejected with the ``RESOURCE`` category before touching the DB.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

from . import encoding
from .errors import Code, LightClientError, StorageError
from .types import Committee, Header

META_KEY = "tip"


@dataclass(frozen=True)
class StoredHeader:
    root: bytes
    round: int
    parent_root: bytes
    body_root: bytes
    timestamp_ms: int
    next_committee_commitment: Optional[bytes]
    header_json: Dict[str, Any]


@dataclass(frozen=True)
class TipState:
    """Immutable snapshot of the client's trusted head.

    ``tip_header_root is None`` means 'not initialized' (no checkpoint yet).
    ``active_committee_commitment`` identifies the committee that authorizes
    the *next* header; after a rotation lands it is the new committee.
    """

    tip_header_root: Optional[bytes]
    active_committee_commitment: Optional[bytes]
    tip_round: int
    tip_timestamp_ms: int
    sequence: int

    def as_snapshot(self) -> "TipState":
        return TipState(
            self.tip_header_root,
            self.active_committee_commitment,
            self.tip_round,
            self.tip_timestamp_ms,
            self.sequence,
        )


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    tip_header_root BLOB,
    tip_round INTEGER NOT NULL DEFAULT 0,
    tip_timestamp_ms INTEGER NOT NULL DEFAULT 0,
    active_committee_commitment BLOB,
    sequence INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO meta (id, tip_header_root, tip_round, tip_timestamp_ms,
                            active_committee_commitment, sequence)
VALUES (1, NULL, 0, 0, NULL, 0);

CREATE TABLE IF NOT EXISTS headers (
    root BLOB PRIMARY KEY,
    round INTEGER NOT NULL,
    parent_root BLOB NOT NULL,
    body_root BLOB NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    next_committee_commitment BLOB,
    header_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_headers_round ON headers(round);
CREATE INDEX IF NOT EXISTS idx_headers_parent ON headers(parent_root);
CREATE INDEX IF NOT EXISTS idx_headers_ts ON headers(timestamp_ms);

CREATE TABLE IF NOT EXISTS committees (
    commitment BLOB PRIMARY KEY,
    total_weight INTEGER NOT NULL,
    encoded BLOB NOT NULL,
    committee_json TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        # The HTTP service handles requests on worker threads, so allow the
        # connection off the creating thread. Writers are serialized by
        # ``_write_lock`` + BEGIN IMMEDIATE; reads are safe under WAL.
        self._write_lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(
                path, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise StorageError(
                f"cannot open database {path!r}: {exc}",
                details={"path": path},
            )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        # WAL makes concurrent readers safe; harmless for :memory:.
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        try:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"schema initialization failed: {exc}")

    def close(self) -> None:
        self._conn.close()

    # -- transactions --------------------------------------------------------
    @contextlib.contextmanager
    def chain_txn(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn
        with self._write_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise StorageError(f"cannot begin transaction: {exc}")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    # -- tip state -----------------------------------------------------------
    def get_tip(self, conn: Optional[sqlite3.Connection] = None) -> TipState:
        if conn is not None:
            return self._read_tip(conn)
        with self._write_lock:
            return self._read_tip(self._conn)

    @staticmethod
    def _read_tip(c: sqlite3.Connection) -> TipState:
        row = c.execute(
            "SELECT tip_header_root, active_committee_commitment, tip_round, "
            "tip_timestamp_ms, sequence FROM meta WHERE id = 1"
        ).fetchone()
        return TipState(
            row["tip_header_root"],
            row["active_committee_commitment"],
            row["tip_round"],
            row["tip_timestamp_ms"],
            row["sequence"],
        )

    def set_tip(
        self,
        conn: sqlite3.Connection,
        tip_root: bytes,
        tip_round: int,
        tip_timestamp_ms: int,
        active_committee_commitment: bytes,
    ) -> None:
        conn.execute(
            "UPDATE meta SET tip_header_root = ?, tip_round = ?, "
            "tip_timestamp_ms = ?, active_committee_commitment = ?, "
            "sequence = sequence + 1 WHERE id = 1",
            (
                tip_root,
                tip_round,
                tip_timestamp_ms,
                active_committee_commitment,
            ),
        )

    def init_tip(
        self,
        conn: sqlite3.Connection,
        tip_root: bytes,
        tip_round: int,
        tip_timestamp_ms: int,
        active_committee_commitment: bytes,
    ) -> None:
        """Install the out-of-band checkpoint tip; refuses if one exists."""
        conn.execute(
            "UPDATE meta SET tip_header_root = ?, tip_round = ?, "
            "tip_timestamp_ms = ?, active_committee_commitment = ?, "
            "sequence = sequence + 1 "
            "WHERE id = 1 AND tip_header_root IS NULL",
            (
                tip_root,
                tip_round,
                tip_timestamp_ms,
                active_committee_commitment,
            ),
        )
        if conn.execute("SELECT changes() AS n").fetchone()["n"] == 0:
            raise StorageError("tip already initialized (checkpoint conflict)")

    # -- headers -------------------------------------------------------------
    @contextlib.contextmanager
    def _cursor(self, conn: Optional[sqlite3.Connection]):
        """Yield a connection to read on; serializes top-level reads across
        threads, but stays re-entrant inside an open write transaction."""
        if conn is not None:
            yield conn
        else:
            with self._write_lock:
                yield self._conn

    def has_header(
        self, root: bytes, conn: Optional[sqlite3.Connection] = None
    ) -> bool:
        with self._cursor(conn) as c:
            row = c.execute(
                "SELECT 1 FROM headers WHERE root = ?", (root,)
            ).fetchone()
        return row is not None

    def get_header(
        self, root: bytes, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[StoredHeader]:
        with self._cursor(conn) as c:
            row = c.execute("SELECT * FROM headers WHERE root = ?", (root,)).fetchone()
        return self._row_to_header(row) if row else None

    def get_child(
        self, parent: bytes, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[StoredHeader]:
        """Return the accepted child of ``parent``. Two distinct children of
        the same trusted parent are never both accepted, so this is at most 1."""
        with self._cursor(conn) as c:
            row = c.execute(
                "SELECT * FROM headers WHERE parent_root = ? LIMIT 1", (parent,)
            ).fetchone()
        return self._row_to_header(row) if row else None

    def get_headers_by_round(
        self, round_index: int, conn: Optional[sqlite3.Connection] = None
    ) -> List[StoredHeader]:
        with self._cursor(conn) as c:
            rows = c.execute(
                "SELECT * FROM headers WHERE round = ? ORDER BY root", (round_index,)
            ).fetchall()
        return [self._row_to_header(r) for r in rows]

    def count_headers(self, conn: Optional[sqlite3.Connection] = None) -> int:
        with self._cursor(conn) as c:
            return c.execute("SELECT COUNT(*) AS n FROM headers").fetchone()["n"]

    def insert_header(
        self, conn: sqlite3.Connection, header: Header, root: bytes
    ) -> None:
        try:
            conn.execute(
                "INSERT INTO headers "
                "(root, round, parent_root, body_root, timestamp_ms, "
                " next_committee_commitment, header_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    root,
                    header.round,
                    header.parent_root,
                    header.body_root,
                    header.timestamp_ms,
                    header.next_committee_commitment,
                    json.dumps(header.to_dict(), separators=(",", ":"), sort_keys=True),
                ),
            )
        except sqlite3.IntegrityError as exc:
            # Duplicate root: idempotent replay is handled by the kernel.
            raise StorageError(f"header insert conflict: {exc}", details={"root": root.hex()})

    # -- committees ----------------------------------------------------------
    def get_committee(
        self, commitment: bytes, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[Committee]:
        with self._cursor(conn) as c:
            row = c.execute(
                "SELECT encoded FROM committees WHERE commitment = ?", (commitment,)
            ).fetchone()
        if row is None:
            return None
        return encoding.decode_committee(row["encoded"])

    def has_committee(
        self, commitment: bytes, conn: Optional[sqlite3.Connection] = None
    ) -> bool:
        with self._cursor(conn) as c:
            row = c.execute(
                "SELECT 1 FROM committees WHERE commitment = ?", (commitment,)
            ).fetchone()
        return row is not None

    def insert_committee(
        self, conn: sqlite3.Connection, committee: Committee, commitment: bytes
    ) -> None:
        conn.execute(
            "INSERT INTO committees (commitment, total_weight, encoded, committee_json) "
            "VALUES (?, ?, ?, ?)",
            (
                commitment,
                committee.total_weight,
                encoding.encode_committee(committee),
                json.dumps(committee.to_dict(), separators=(",", ":"), sort_keys=True),
            ),
        )

    # -- integrity / diagnostics --------------------------------------------
    def assert_consistent(self) -> None:
        """Quick integrity check used by the health endpoint and tests."""
        try:
            self._conn.execute("PRAGMA integrity_check").fetchone()
            tip = self.get_tip()
            if tip.tip_header_root is not None and not self.has_header(
                tip.tip_header_root
            ):
                raise StorageError(
                    "trusted tip points at a header that is not in the store",
                    details={"tip": tip.tip_header_root.hex()},
                )
        except sqlite3.Error as exc:
            raise StorageError(f"integrity check failed: {exc}")

    @staticmethod
    def _row_to_header(row: sqlite3.Row) -> StoredHeader:
        ncc = row["next_committee_commitment"]
        return StoredHeader(
            root=row["root"],
            round=row["round"],
            parent_root=row["parent_root"],
            body_root=row["body_root"],
            timestamp_ms=row["timestamp_ms"],
            next_committee_commitment=ncc,
            header_json=json.loads(row["header_json"]),
        )
