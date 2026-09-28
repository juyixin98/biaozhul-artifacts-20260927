"""Execution kernel: physical file storage + independent cleanup ledger.

This layer owns *filesystem mechanics only* — it knows nothing about
snapshots or commit conflict rules:

* :class:`FileStorage` stages files (complete write + fsync + rename), then
  publishes each staged file into the warehouse with an atomic
  ``os.replace`` onto a fresh, immutable path.
* :class:`CleanupLedger` is a **separate** SQLite ledger recording every
  cleanup action (failed staging temp files, partial-publish orphans,
  stale staged files).  It is deliberately independent from the metadata
  database: a metadata failure can never erase the audit trail of files
  that must be reclaimed.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from app.kernel.errors import ErrorCategory, ServiceError


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StagedFile:
    """A file fully written into staging, not yet visible in any snapshot."""

    staged_path: Path
    request_id: str
    source_name: str
    size_bytes: int
    fingerprint: str


@dataclass(frozen=True)
class PublishedFile:
    """A staged file that has been atomically renamed into the warehouse."""

    staged: StagedFile
    published_relpath: str


@dataclass(frozen=True)
class CleanupRecord:
    record_id: str
    request_id: str
    table_name: str
    kind: str
    path: str
    status: str
    recorded_at: float
    removed_at: float | None
    error: str | None


CLEANUP_KIND_STAGED_TEMP = "STAGED_TEMP_FILE"
CLEANUP_KIND_PARTIAL_PUBLISH = "PARTIAL_PUBLISH_ORPHAN"
CLEANUP_KIND_STALE_STAGED = "STALE_STAGED_FILE"
CLEANUP_KIND_WAREHOUSE_ORPHAN = "WAREHOUSE_ORPHAN"

STATUS_PENDING = "PENDING"
STATUS_REMOVED = "REMOVED"
STATUS_FAILED = "FAILED"


# ---------------------------------------------------------------------------
# File storage
# ---------------------------------------------------------------------------


class FileStorage:
    def __init__(self, warehouse_dir: Path, staging_dir: Path) -> None:
        self.warehouse_dir = Path(warehouse_dir)
        self.staging_dir = Path(staging_dir)
        self.warehouse_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    # -- staging -----------------------------------------------------------

    def stage(
        self, source_path: Path, request_id: str, fingerprint: str
    ) -> StagedFile:
        """Stage an already-validated local file.

        The caller (parquet adapter) guarantees readability; here we only do
        the complete-temp-write + fsync + atomic rename dance.
        """
        source_path = Path(source_path)
        staged_name = f"req-{request_id}__{fingerprint}__{source_path.name}"
        final_path = self.staging_dir / staged_name
        if final_path.exists():
            # Idempotent retry with the same request id and same content.
            return StagedFile(
                staged_path=final_path,
                request_id=request_id,
                source_name=source_path.name,
                size_bytes=final_path.stat().st_size,
                fingerprint=fingerprint,
            )

        tmp_path = self.staging_dir / f".writing-{uuid.uuid4().hex}"
        try:
            with open(tmp_path, "wb") as dst, open(source_path, "rb") as src:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            tmp_path.replace(final_path)
            self._fsync_dir(self.staging_dir)
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise ServiceError(
                ErrorCategory.STAGING_FAILED,
                f"staging failed for {source_path.name}: {exc}",
                details={
                    "file": source_path.name,
                    "cause": type(exc).__name__,
                    "request_id": request_id,
                },
                request_id=request_id,
            ) from exc

        return StagedFile(
            staged_path=final_path,
            request_id=request_id,
            source_name=source_path.name,
            size_bytes=final_path.stat().st_size,
            fingerprint=fingerprint,
        )

    # -- publishing --------------------------------------------------------

    def publish_destination(self, table_name: str) -> str:
        """Fresh immutable relative path inside the warehouse."""
        return f"{table_name}/{uuid.uuid4().hex}.parquet"

    def publish_one(
        self, staged: StagedFile, relpath: str
    ) -> PublishedFile:
        """Atomically move one staged file onto its warehouse path."""
        dest = self.warehouse_dir / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():  # UUID collision, practically impossible; refuse.
            raise ServiceError(
                ErrorCategory.INDETERMINATE,
                "warehouse destination already exists",
                details={"relpath": relpath, "request_id": staged.request_id},
                request_id=staged.request_id,
            )
        try:
            staged.staged_path.replace(dest)
            self._fsync_dir(dest.parent)
        except OSError as exc:
            raise ServiceError(
                ErrorCategory.INDETERMINATE,
                f"publish failed for {staged.source_name}: {exc}",
                details={
                    "file": staged.source_name,
                    "cause": type(exc).__name__,
                    "request_id": staged.request_id,
                },
                request_id=staged.request_id,
            ) from exc
        return PublishedFile(staged=staged, published_relpath=relpath)

    # -- scanning ----------------------------------------------------------

    def warehouse_files(self, table_name: str) -> set[str]:
        """Relative paths of every regular file under the table directory."""
        table_dir = self.warehouse_dir / table_name
        found: set[str] = set()
        if not table_dir.exists():
            return found
        for path in table_dir.rglob("*"):
            if path.is_file():
                found.add(str(path.relative_to(self.warehouse_dir)))
        return found

    def warehouse_dirs(self, table_name: str) -> list[str]:
        """Relative directory paths under the table directory."""
        table_dir = self.warehouse_dir / table_name
        found: list[str] = []
        if not table_dir.exists():
            return found
        for path in sorted(table_dir.rglob("*")):
            if path.is_dir():
                found.append(str(path.relative_to(self.warehouse_dir)))
        return found

    def staged_files(self) -> set[str]:
        return {p.name for p in self.staging_dir.iterdir() if p.is_file()}

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Independent cleanup ledger
# ---------------------------------------------------------------------------


class CleanupLedger:
    """Append-first audit ledger in its own SQLite database."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.db_path, isolation_level=None, check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cleanup_records (
                    record_id    TEXT PRIMARY KEY,
                    request_id   TEXT NOT NULL,
                    table_name   TEXT NOT NULL,
                    kind         TEXT NOT NULL,
                    path         TEXT NOT NULL,
                    status       TEXT NOT NULL,
                    recorded_at  REAL NOT NULL,
                    removed_at   REAL,
                    error        TEXT
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cleanup_status "
                "ON cleanup_records(status)"
            )

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
            except Exception:
                raise

    def record_pending(
        self,
        *,
        request_id: str,
        table_name: str,
        kind: str,
        path: str | Path,
    ) -> str:
        record_id = "cln-" + uuid.uuid4().hex
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO cleanup_records "
                "(record_id, request_id, table_name, kind, path, status, "
                "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    request_id,
                    table_name,
                    kind,
                    str(path),
                    STATUS_PENDING,
                    time.time(),
                ),
            )
        return record_id

    def mark(self, record_id: str, status: str, error: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE cleanup_records SET status = ?, removed_at = ?, "
                "error = ? WHERE record_id = ?",
                (
                    status,
                    time.time() if status == STATUS_REMOVED else None,
                    error,
                    record_id,
                ),
            )

    def list_records(self, status: str | None = None) -> list[CleanupRecord]:
        with self._lock:
            if status is None:
                cur = self._conn.execute(
                    "SELECT record_id, request_id, table_name, kind, path, "
                    "status, recorded_at, removed_at, error "
                    "FROM cleanup_records ORDER BY recorded_at"
                )
            else:
                cur = self._conn.execute(
                    "SELECT record_id, request_id, table_name, kind, path, "
                    "status, recorded_at, removed_at, error "
                    "FROM cleanup_records WHERE status = ? ORDER BY recorded_at",
                    (status,),
                )
            return [CleanupRecord(*row) for row in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def remove_path(path: Path, ledger: CleanupLedger, record_id: str) -> bool:
    """Remove a file/dir, updating its ledger record. Returns success."""
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            Path(path).unlink(missing_ok=True)
        ledger.mark(record_id, STATUS_REMOVED)
        return True
    except OSError as exc:
        ledger.mark(record_id, STATUS_FAILED, error=f"{type(exc).__name__}: {exc}")
        return False
