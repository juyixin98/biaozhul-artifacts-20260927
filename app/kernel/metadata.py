"""Metadata transaction store.

Owns *metadata transactions only*: tables, the linear snapshot chain,
materialised per-snapshot manifest rows, partition-change history and the
idempotent commit log.  It never touches parquet files or the warehouse.

Concurrency model
-----------------
Writers are serialised by ``BEGIN IMMEDIATE`` (a single SQLite writer lock)
and, at the service layer, by a per-table lock.  Inside one exclusive
transaction the store sees the current HEAD, applies the declared merge
rules, and either inserts a new snapshot or records a terminal rejection.

Merge rules (simplified, explicitly *not* full Iceberg)
-------------------------------------------------------
* APPEND whose touched partitions are disjoint from every commit made since
  the client's base snapshot is rebased onto HEAD and accepted.
* Any operation touching a partition changed since the base snapshot is a
  hard conflict (``CONFLICT_OVERLAPPING_PARTITION``) — never last-writer-wins.
* OVERWRITE replaces exactly the files belonging to its target partitions.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from app.adapters.parquet import Partition
from app.kernel.errors import ErrorCategory, ServiceError

OP_APPEND = "APPEND"
OP_OVERWRITE = "OVERWRITE"
OP_ROOT = "ROOT"

STATUS_PENDING = "PENDING"
STATUS_COMMITTED = "COMMITTED"
STATUS_REJECTED = "REJECTED"
STATUS_FAILED = "FAILED"

UNPARTITIONED_KEY = ""


@dataclass(frozen=True)
class NewFileEntry:
    """One file a request wants to attach to the next snapshot."""

    relpath: str
    staged_path: Path
    source_name: str
    fingerprint: str
    row_count: int
    partition_keys: tuple[str, ...]

    def partitions_json(self) -> str:
        return json.dumps(list(self.partition_keys))


@dataclass(frozen=True)
class ManifestEntry:
    file_relpath: str
    row_count: int
    partition_keys: tuple[str, ...]


@dataclass(frozen=True)
class CommitRecord:
    request_id: str
    table_name: str
    operation: str | None
    status: str
    base_snapshot_id: int | None
    final_snapshot_id: int | None
    request_fingerprint: str | None
    error_category: str | None
    error_message: str | None
    rebased: bool
    attempts: int
    created_at: float
    finished_at: float | None


def partition_keys(parts: Sequence[Partition]) -> tuple[str, ...]:
    keys = tuple(p.key() for p in parts)
    return keys if keys else (UNPARTITIONED_KEY,)


class MetadataStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._busy_retries = 50
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._init_schema()

    # ------------------------------------------------------------------ setup

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tables (
                    table_name         TEXT PRIMARY KEY,
                    partition_spec     TEXT NOT NULL,
                    schema_json        TEXT,
                    current_snapshot_id INTEGER NOT NULL,
                    created_at         REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_name         TEXT NOT NULL,
                    parent_snapshot_id INTEGER,
                    operation          TEXT NOT NULL,
                    commit_id          TEXT NOT NULL UNIQUE,
                    created_at         REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS manifest_rows (
                    snapshot_id    INTEGER NOT NULL,
                    table_name     TEXT NOT NULL,
                    file_relpath   TEXT NOT NULL,
                    row_count      INTEGER NOT NULL,
                    partitions_json TEXT NOT NULL,
                    PRIMARY KEY (snapshot_id, file_relpath)
                );

                CREATE TABLE IF NOT EXISTS partition_changes (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_name  TEXT NOT NULL,
                    partition_key TEXT NOT NULL,
                    snapshot_id INTEGER NOT NULL,
                    op          TEXT NOT NULL,
                    UNIQUE (table_name, partition_key, snapshot_id)
                );

                CREATE TABLE IF NOT EXISTS commits (
                    request_id          TEXT PRIMARY KEY,
                    table_name          TEXT NOT NULL,
                    operation           TEXT,
                    status              TEXT NOT NULL,
                    base_snapshot_id    INTEGER,
                    final_snapshot_id  INTEGER,
                    request_fingerprint TEXT,
                    error_category      TEXT,
                    error_message       TEXT,
                    rebased             INTEGER NOT NULL DEFAULT 0,
                    attempts            INTEGER NOT NULL DEFAULT 1,
                    created_at          REAL NOT NULL,
                    finished_at         REAL
                );
                """
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ tables

    def create_table(self, table_name: str, partition_spec: tuple[str, ...]) -> int:
        """Create a table and its empty ROOT snapshot. Returns root id."""
        with self._lock, self._begin_immediate() as conn:
            existing = conn.execute(
                "SELECT 1 FROM tables WHERE table_name = ?", (table_name,)
            ).fetchone()
            if existing is not None:
                raise ServiceError(
                    ErrorCategory.VALIDATION,
                    f"table {table_name!r} already exists",
                    details={"table": table_name},
                )
            now = time.time()
            cur = conn.execute(
                "INSERT INTO snapshots (table_name, parent_snapshot_id, "
                "operation, commit_id, created_at) VALUES (?, NULL, ?, ?, ?)",
                (table_name, OP_ROOT, f"root:{table_name}", now),
            )
            root_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO tables (table_name, partition_spec, schema_json, "
                "current_snapshot_id, created_at) VALUES (?, ?, NULL, ?, ?)",
                (table_name, json.dumps(list(partition_spec)), root_id, now),
            )
            return root_id

    def get_table(self, table_name: str) -> sqlite3.Row:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tables WHERE table_name = ?", (table_name,)
            ).fetchone()
        if row is None:
            raise ServiceError(
                ErrorCategory.NOT_FOUND,
                f"table {table_name!r} does not exist",
                details={"table": table_name},
            )
        return row

    def table_spec(self, table_name: str) -> tuple[str, ...]:
        return tuple(json.loads(self.get_table(table_name)["partition_spec"]))

    def set_schema(self, table_name: str, schema_json: str) -> None:
        """Update schema. Caller MUST hold an open exclusive transaction."""
        self._conn.execute(
            "UPDATE tables SET schema_json = ? WHERE table_name = ?",
            (schema_json, table_name),
        )

    # ------------------------------------------------------------- transactions

    @contextmanager
    def _begin_immediate(self) -> Iterator[sqlite3.Connection]:
        # busy_timeout handles most contention; retry explicitly for the
        # residual SQLITE_BUSY window.
        last_exc: sqlite3.OperationalError | None = None
        for _ in range(self._busy_retries):
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:  # pragma: no cover - timing
                last_exc = exc
                time.sleep(0.02)
        else:  # pragma: no cover - timing
            raise ServiceError(
                ErrorCategory.CONFLICT_RETRY_EXHAUSTED,
                f"could not acquire metadata write lock: {last_exc}",
            )
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    @contextmanager
    def exclusive(self) -> Iterator["Transaction"]:
        """Acquire the store-wide lock and open an exclusive transaction.

        The transaction commits on clean exit and rolls back on error unless
        the transaction object has already terminated it.
        """
        self._lock.acquire()
        tx = Transaction(self._conn, self)
        try:
            for attempt in range(self._busy_retries):
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError:  # pragma: no cover - timing
                    time.sleep(0.02)
            else:  # pragma: no cover - timing
                raise ServiceError(
                    ErrorCategory.CONFLICT_RETRY_EXHAUSTED,
                    "could not acquire metadata write lock",
                )
            tx._open = True
            yield tx
            if tx._open:
                self._conn.execute("COMMIT")
                tx._open = False
        except BaseException:
            if tx._open:
                self._conn.execute("ROLLBACK")
                tx._open = False
            raise
        finally:
            self._lock.release()

    # ----------------------------------------------------------------- queries

    def head_snapshot_id(self, table_name: str) -> int:
        return int(self.get_table(table_name)["current_snapshot_id"])

    def snapshot_entries(self, snapshot_id: int) -> list[ManifestEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT file_relpath, row_count, partitions_json "
                "FROM manifest_rows WHERE snapshot_id = ? ORDER BY file_relpath",
                (snapshot_id,),
            ).fetchall()
        return [
            ManifestEntry(
                file_relpath=r["file_relpath"],
                row_count=r["row_count"],
                partition_keys=tuple(json.loads(r["partitions_json"])),
            )
            for r in rows
        ]

    def all_referenced_relpaths(self, table_name: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT file_relpath FROM manifest_rows "
                "WHERE table_name = ?",
                (table_name,),
            ).fetchall()
        return {r["file_relpath"] for r in rows}

    def list_snapshots(self, table_name: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT snapshot_id, parent_snapshot_id, operation, "
                    "commit_id, created_at FROM snapshots "
                    "WHERE table_name = ? ORDER BY snapshot_id",
                    (table_name,),
                ).fetchall()
            )

    def get_snapshot(self, table_name: str, snapshot_id: int) -> sqlite3.Row:
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot_id, parent_snapshot_id, operation, commit_id, "
                "created_at FROM snapshots WHERE table_name = ? AND snapshot_id = ?",
                (table_name, snapshot_id),
            ).fetchone()
        if row is None:
            raise ServiceError(
                ErrorCategory.NOT_FOUND,
                f"snapshot {snapshot_id} not found in table {table_name!r}",
                details={"table": table_name, "snapshot_id": snapshot_id},
            )
        return row

    def get_commit(self, request_id: str) -> CommitRecord | None:
        with self._lock:
            return self._fetch_commit(request_id)

    def list_commits(self, table_name: str | None = None) -> list[CommitRecord]:
        with self._lock:
            if table_name is None:
                rows = self._conn.execute(
                    "SELECT * FROM commits ORDER BY created_at"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM commits WHERE table_name = ? ORDER BY created_at",
                    (table_name,),
                ).fetchall()
        return [self._row_to_commit(r) for r in rows]

    def pending_commits(self) -> list[CommitRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM commits WHERE status = ? ORDER BY created_at",
                (STATUS_PENDING,),
            ).fetchall()
        return [self._row_to_commit(r) for r in rows]

    def _fetch_commit(self, request_id: str) -> CommitRecord | None:
        row = self._conn.execute(
            "SELECT * FROM commits WHERE request_id = ?", (request_id,)
        ).fetchone()
        return self._row_to_commit(row) if row else None

    @staticmethod
    def _row_to_commit(r: sqlite3.Row) -> CommitRecord:
        return CommitRecord(
            request_id=r["request_id"],
            table_name=r["table_name"],
            operation=r["operation"],
            status=r["status"],
            base_snapshot_id=r["base_snapshot_id"],
            final_snapshot_id=r["final_snapshot_id"],
            request_fingerprint=r["request_fingerprint"],
            error_category=r["error_category"],
            error_message=r["error_message"],
            rebased=bool(r["rebased"]),
            attempts=r["attempts"],
            created_at=r["created_at"],
            finished_at=r["finished_at"],
        )

    def record_terminal_failure(
        self,
        *,
        request_id: str,
        table_name: str,
        operation: str | None,
        base_snapshot_id: int | None,
        request_fingerprint: str,
        category: ErrorCategory,
        message: str,
        status: str = STATUS_FAILED,
    ) -> None:
        """Persist a terminal failure/rejection before the txn window."""
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO commits (request_id, table_name, operation, "
                "status, base_snapshot_id, final_snapshot_id, "
                "request_fingerprint, error_category, error_message, "
                "created_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?) "
                "ON CONFLICT(request_id) DO NOTHING",
                (
                    request_id,
                    table_name,
                    operation,
                    status,
                    base_snapshot_id,
                    request_fingerprint,
                    category.value,
                    message,
                    now,
                    now,
                ),
            )


class Transaction:
    """Open metadata transaction; used only while ``MetadataStore.exclusive``."""

    def __init__(self, conn: sqlite3.Connection, store: MetadataStore) -> None:
        self._conn = conn
        self._store = store
        self._open = False

    # ------------------------------------------------------------- idempotency

    def existing_outcome(
        self, request_id: str, request_fingerprint: str
    ) -> CommitRecord | None:
        """Return a stored terminal outcome for this exact request, if any.

        A ``request_id`` reused with a *different* payload is a client bug and
        rejected, rather than silently returning the old outcome.
        """
        record = self._store._fetch_commit(request_id)
        if record is None or record.status == STATUS_PENDING:
            return None
        if record.request_fingerprint != request_fingerprint:
            raise ServiceError(
                ErrorCategory.VALIDATION,
                "request_id was already used with a different payload",
                details={
                    "request_id": request_id,
                    "stored_fingerprint": record.request_fingerprint,
                },
                request_id=request_id,
            )
        return record

    def insert_pending(
        self,
        *,
        request_id: str,
        table_name: str,
        operation: str,
        base_snapshot_id: int,
        request_fingerprint: str,
        attempts: int,
    ) -> None:
        self._conn.execute(
            "INSERT INTO commits (request_id, table_name, operation, status, "
            "base_snapshot_id, request_fingerprint, attempts, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                table_name,
                operation,
                STATUS_PENDING,
                base_snapshot_id,
                request_fingerprint,
                attempts,
                time.time(),
            ),
        )

    # ------------------------------------------------------------------ plan

    def plan_and_apply(
        self,
        *,
        table_name: str,
        partition_spec: tuple[str, ...],
        operation: str,
        request_id: str,
        base_snapshot_id: int,
        entries: Sequence[NewFileEntry],
    ) -> tuple[int, bool]:
        """Validate against HEAD, then insert snapshot + manifest rows.

        Returns ``(new_snapshot_id, rebased)``.  On conflict commits a terminal
        REJECTED outcome and raises :class:`ServiceError`.
        """
        conn = self._conn
        head_id = conn.execute(
            "SELECT current_snapshot_id FROM tables WHERE table_name = ?",
            (table_name,),
        ).fetchone()[0]

        self._check_base_exists(table_name, base_snapshot_id)

        touched = self._touched_keys(operation, entries)
        changed = self._changed_keys_since(table_name, base_snapshot_id, head_id)
        overlap = sorted(set(touched) & changed)
        rebased = head_id != base_snapshot_id

        if overlap:
            self._reject(
                request_id=request_id,
                category=ErrorCategory.CONFLICT_OVERLAPPING_PARTITION,
                message=(
                    f"{operation} conflicts: partition(s) {overlap} changed "
                    f"since base snapshot {base_snapshot_id}; HEAD is now "
                    f"{head_id}"
                ),
                details={
                    "table": table_name,
                    "base_snapshot_id": base_snapshot_id,
                    "head_snapshot_id": head_id,
                    "overlapping_partitions": overlap,
                    "changed_partitions": sorted(changed),
                    "requested_partitions": sorted(touched),
                },
            )

        parent_entries = self._store.snapshot_entries(head_id)
        new_rows = self._materialize(
            operation=operation,
            touched=touched,
            parent_entries=parent_entries,
            entries=entries,
            partition_spec=partition_spec,
        )

        now = time.time()
        cur = conn.execute(
            "INSERT INTO snapshots (table_name, parent_snapshot_id, "
            "operation, commit_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (table_name, head_id, operation, request_id, now),
        )
        snapshot_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO manifest_rows (snapshot_id, table_name, file_relpath, "
            "row_count, partitions_json) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    snapshot_id,
                    table_name,
                    e.file_relpath,
                    e.row_count,
                    json.dumps(list(e.partition_keys)),
                )
                for e in new_rows
            ],
        )
        change_op = "REPLACE" if operation == OP_OVERWRITE else "ADD"
        conn.executemany(
            "INSERT INTO partition_changes (table_name, partition_key, "
            "snapshot_id, op) VALUES (?, ?, ?, ?)",
            [(table_name, k, snapshot_id, change_op) for k in sorted(touched)],
        )
        conn.execute(
            "UPDATE tables SET current_snapshot_id = ? WHERE table_name = ?",
            (snapshot_id, table_name),
        )
        conn.execute(
            "UPDATE commits SET status = ?, final_snapshot_id = ?, "
            "rebased = ?, finished_at = ? WHERE request_id = ?",
            (STATUS_COMMITTED, snapshot_id, int(rebased), now, request_id),
        )
        return snapshot_id, rebased

    def mark_failed(
        self, request_id: str, category: ErrorCategory, message: str
    ) -> None:
        self._conn.execute(
            "UPDATE commits SET status = ?, error_category = ?, "
            "error_message = ?, finished_at = ? WHERE request_id = ?",
            (STATUS_FAILED, category.value, message, time.time(), request_id),
        )

    def _reject(
        self,
        *,
        request_id: str,
        category: ErrorCategory,
        message: str,
        details: dict,
    ) -> None:
        # Commit the terminal REJECTED outcome so retries report it
        # idempotently, then raise.
        self._conn.execute(
            "UPDATE commits SET status = ?, error_category = ?, "
            "error_message = ?, finished_at = ? WHERE request_id = ?",
            (STATUS_REJECTED, category.value, message, time.time(), request_id),
        )
        self._conn.execute("COMMIT")
        self._open = False
        raise ServiceError(
            category, message, details=details, request_id=request_id
        )

    # ------------------------------------------------------------------ helpers

    def _check_base_exists(self, table_name: str, base_snapshot_id: int) -> None:
        row = self._conn.execute(
            "SELECT 1 FROM snapshots WHERE table_name = ? AND snapshot_id = ?",
            (table_name, base_snapshot_id),
        ).fetchone()
        if row is None:
            raise ServiceError(
                ErrorCategory.NOT_FOUND,
                f"base snapshot {base_snapshot_id} not found",
                details={"table": table_name, "snapshot_id": base_snapshot_id},
            )

    @staticmethod
    def _touched_keys(
        operation: str, entries: Sequence[NewFileEntry]
    ) -> set[str]:
        keys: set[str] = set()
        for e in entries:
            keys.update(e.partition_keys)
        if not keys:
            raise ServiceError(
                ErrorCategory.VALIDATION,
                "commit contains no files",
            )
        if operation == OP_OVERWRITE:
            # Each overwrite file must belong to exactly one partition, so a
            # physical file can never straddle a replaced and a kept
            # partition (its rows could not be removed from an immutable file).
            for e in entries:
                if len(e.partition_keys) != 1:
                    raise ServiceError(
                        ErrorCategory.VALIDATION,
                        "OVERWRITE files must each belong to exactly one "
                        f"partition; {e.source_name} spans "
                        f"{list(e.partition_keys)}",
                        details={
                            "file": e.source_name,
                            "partitions": list(e.partition_keys),
                        },
                    )
        return keys

    def _changed_keys_since(
        self, table_name: str, base_id: int, head_id: int
    ) -> set[str]:
        """Partitions touched by snapshots on the chain base->head exclusive."""
        if base_id == head_id:
            return set()
        changed: set[str] = set()
        walk = head_id
        # Linear chain; walk parents until we reach the base.
        seen: set[int] = set()
        while walk != base_id:
            if walk in seen:  # pragma: no cover - defensive cycle guard
                raise ServiceError(ErrorCategory.INDETERMINATE, "snapshot cycle")
            seen.add(walk)
            rows = self._conn.execute(
                "SELECT partition_key FROM partition_changes "
                "WHERE table_name = ? AND snapshot_id = ?",
                (table_name, walk),
            ).fetchall()
            changed.update(r["partition_key"] for r in rows)
            parent = self._conn.execute(
                "SELECT parent_snapshot_id FROM snapshots WHERE snapshot_id = ?",
                (walk,),
            ).fetchone()[0]
            if parent is None:
                raise ServiceError(
                    ErrorCategory.VALIDATION,
                    f"base snapshot {base_id} is not an ancestor of HEAD "
                    f"{head_id}",
                )
            walk = parent
        return changed

    @staticmethod
    def _materialize(
        *,
        operation: str,
        touched: set[str],
        parent_entries: Sequence[ManifestEntry],
        entries: Sequence[NewFileEntry],
        partition_spec: tuple[str, ...],
    ) -> list[ManifestEntry]:
        if operation == OP_APPEND:
            rows = list(parent_entries)
            existing_paths = {e.file_relpath for e in rows}
            for e in entries:
                if e.relpath in existing_paths:
                    raise ServiceError(
                        ErrorCategory.VALIDATION,
                        f"duplicate file path in manifest: {e.relpath}",
                        details={"relpath": e.relpath},
                    )
                rows.append(
                    ManifestEntry(
                        file_relpath=e.relpath,
                        row_count=e.row_count,
                        partition_keys=e.partition_keys,
                    )
                )
            return rows

        # OVERWRITE: drop old files intersecting target partitions, refuse if
        # such a file also carries rows for a non-target partition.
        kept: list[ManifestEntry] = []
        for old in parent_entries:
            hits = set(old.partition_keys) & touched
            if not hits:
                kept.append(old)
            elif set(old.partition_keys) - touched:
                raise ServiceError(
                    ErrorCategory.VALIDATION,
                    "cannot overwrite partition(s): an existing immutable "
                    "file also contains rows of a non-target partition",
                    details={
                        "file": old.file_relpath,
                        "file_partitions": list(old.partition_keys),
                        "target_partitions": sorted(touched),
                    },
                )
        for e in entries:
            kept.append(
                ManifestEntry(
                    file_relpath=e.relpath,
                    row_count=e.row_count,
                    partition_keys=e.partition_keys,
                )
            )
        return kept
