"""Metadata transaction service.

Orchestrates the four layers for one commit:

  adapters (read/validate parquet)
    -> kernel storage (stage, then atomic publish)
      -> kernel metadata (one exclusive SQLite transaction: conflict check,
         snapshot + manifest materialisation, idempotency log)
        -> diagnostics (why accepted / rejected / indeterminate)

Declared merge policy
---------------------
* APPEND on partitions disjoint from concurrent commits is *rebased* onto
  the new HEAD inside one exclusive transaction.
* APPEND / OVERWRITE touching a partition changed since the client's base is
  rejected with ``CONFLICT_OVERLAPPING_PARTITION``.  Never last-writer-wins.
* Files are fully written (temp + fsync + rename) before they are published;
  every failed file gets its own row in the independent cleanup ledger.
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from app.adapters.parquet import (
    FileFacts,
    copy_to_staging,
    read_file_facts,
)
from app.adapters.schema import CanonicalSchema
from app.kernel.errors import ErrorCategory, ServiceError
from app.kernel.metadata import (
    STATUS_COMMITTED,
    STATUS_FAILED as _META_STATUS_FAILED,
    STATUS_PENDING as _META_STATUS_PENDING,
    STATUS_REJECTED,
    NewFileEntry,
    partition_keys,
)
from app.kernel.metadata import MetadataStore
from app.kernel.metadata import OP_APPEND, OP_OVERWRITE
from app.kernel.storage import (
    CLEANUP_KIND_PARTIAL_PUBLISH,
    CLEANUP_KIND_STALE_STAGED,
    CLEANUP_KIND_WAREHOUSE_ORPHAN,
    CleanupLedger,
    FileStorage,
    remove_path,
)
from app.services.diagnostics import Diagnostics

_VALID_OPERATIONS = {OP_APPEND, OP_OVERWRITE}


@dataclass(frozen=True)
class FileRequest:
    source_path: Path


@dataclass(frozen=True)
class CommitResult:
    request_id: str
    table_name: str
    operation: str
    status: str
    base_snapshot_id: int
    head_snapshot_id: int
    snapshot_id: int | None
    rebased: bool
    replayed: bool
    attempts: int
    files: list[dict]
    total_row_count: int
    partition_keys: list[str]
    error_category: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "table": self.table_name,
            "operation": self.operation,
            "status": self.status,
            "base_snapshot_id": self.base_snapshot_id,
            "head_before_snapshot_id": self.head_snapshot_id,
            "snapshot_id": self.snapshot_id,
            "rebased": self.rebased,
            "replayed": self.replayed,
            "attempts": self.attempts,
            "files": self.files,
            "total_row_count": self.total_row_count,
            "partition_keys": self.partition_keys,
            "error_category": self.error_category,
            "error_message": self.error_message,
        }


class TableService:
    def __init__(
        self,
        store: MetadataStore,
        storage: FileStorage,
        ledger: CleanupLedger,
        diagnostics: Diagnostics,
        max_attempts: int = 3,
    ) -> None:
        self.store = store
        self.storage = storage
        self.ledger = ledger
        self.diag = diagnostics
        self.max_attempts = max_attempts
        # Per-table serialisation; SQLite's IMMEDIATE lock is the real guard,
        # this just avoids needless write-lock contention between threads.
        self._table_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, table: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._table_locks.get(table)
            if lock is None:
                lock = threading.Lock()
                self._table_locks[table] = lock
            return lock

    # ------------------------------------------------------------------ tables

    def create_table(self, table_name: str, partition_spec: Sequence[str]) -> dict:
        spec = tuple(partition_spec)
        self._validate_name(table_name)
        for col in spec:
            if not col:
                raise ServiceError(
                    ErrorCategory.VALIDATION, "partition column name is empty"
                )
        root_id = self.store.create_table(table_name, spec)
        self.diag.accepted(
            "req-create-" + hashlib.sha256(table_name.encode()).hexdigest()[:10],
            "table.created",
            table=table_name,
            root_snapshot_id=root_id,
            partition_spec=list(spec),
        )
        return {
            "table": table_name,
            "partition_spec": list(spec),
            "root_snapshot_id": root_id,
        }

    @staticmethod
    def _validate_name(table_name: str) -> None:
        if not table_name or "/" in table_name or table_name.startswith((".", "_")):
            raise ServiceError(
                ErrorCategory.VALIDATION,
                f"invalid table name: {table_name!r}",
            )

    # ------------------------------------------------------------------ commit

    def commit(
        self,
        *,
        table_name: str,
        operation: str,
        request_id: str | None,
        source_paths: Sequence[Path],
        base_snapshot_id: int | None = None,
    ) -> CommitResult:
        if operation not in _VALID_OPERATIONS:
            raise ServiceError(
                ErrorCategory.VALIDATION,
                f"operation must be one of {sorted(_VALID_OPERATIONS)}",
                details={"operation": operation},
            )
        if not source_paths:
            raise ServiceError(
                ErrorCategory.VALIDATION, "commit must include at least one file"
            )

        table_row = self.store.get_table(table_name)
        spec = tuple(json.loads(table_row["partition_spec"]))
        if base_snapshot_id is None:
            base_snapshot_id = int(table_row["current_snapshot_id"])

        rid = request_id or "req-" + hashlib.sha256(
            f"{table_name}:{operation}".encode()
        ).hexdigest()[:12]
        if not rid.startswith("req-"):
            raise ServiceError(
                ErrorCategory.VALIDATION,
                "request_id must start with 'req-'",
                details={"request_id": rid},
            )

        # ---- 0. hash every file's raw bytes first --------------------------
        # Content hashing does not require valid parquet, so the payload
        # fingerprint is identical whether the request later fails at
        # validation/staging or succeeds: that is what makes lost-response
        # replay return the *same* stored outcome on every path.
        content_fps: list[str] = []
        for raw in source_paths:
            content_fps.append(_sha256_file_or_missing(Path(raw)))
        payload_fp = _payload_fingerprint(
            table_name, operation, base_snapshot_id, content_fps
        )

        prior = self.store.get_commit(rid)
        if (
            prior is not None
            and prior.status in {_META_STATUS_FAILED, STATUS_REJECTED}
        ):
            # Lost-response retry of a request that failed before/outside the
            # commit transaction: replay the stored terminal category.
            if prior.request_fingerprint != payload_fp:
                raise ServiceError(
                    ErrorCategory.VALIDATION,
                    "request_id was already used with a different payload",
                    details={"request_id": rid},
                    request_id=rid,
                )
            category = (
                ErrorCategory(prior.error_category)
                if prior.error_category
                else ErrorCategory.INDETERMINATE
            )
            self.diag.rejected(
                rid, "commit.replayed_prestage_failure",
                prior.error_message or "stored failure",
                table=table_name, category=category.value,
            )
            raise ServiceError(
                category,
                prior.error_message or "request previously failed",
                details={"replayed": True},
                request_id=rid,
            )

        # ---- 1. read + validate every file (nothing is published yet) ------
        facts: list[FileFacts] = []
        table_schema = (
            CanonicalSchema.from_json(table_row["schema_json"])
            if table_row["schema_json"]
            else None
        )
        try:
            for raw in source_paths:
                path = Path(raw)
                fact = read_file_facts(path, spec)
                table_schema = self._schema_after(fact, table_schema, spec)
                facts.append(fact)
        except ServiceError as exc:
            # Pre-staging rejection: carry the caller's request id on the
            # error and persist a terminal outcome so an identical retry
            # reports the same category (lost-response semantics).
            terminal_status = (
                STATUS_REJECTED
                if exc.category is ErrorCategory.VALIDATION
                else _META_STATUS_FAILED
            )
            self.store.record_terminal_failure(
                request_id=rid,
                table_name=table_name,
                operation=operation,
                base_snapshot_id=base_snapshot_id,
                request_fingerprint=payload_fp,
                category=exc.category,
                message=exc.message,
                status=terminal_status,
            )
            self.diag.rejected(
                rid, "commit.validation_failed", exc.message,
                table=table_name, category=exc.category.value,
                base_snapshot_id=base_snapshot_id, details=exc.details,
            )
            raise ServiceError(
                exc.category, exc.message,
                details=exc.details, request_id=rid,
            ) from exc

        # One file = one (request, index) deterministic published identity.
        relpaths = [
            f"{table_name}/{rid}/{i:03d}-{f.source_path.name}"
            for i, f in enumerate(facts)
        ]

        self.diag.emit(
            request_id=rid,
            event="commit.received",
            table=table_name,
            operation=operation,
            base_snapshot_id=base_snapshot_id,
            file_count=len(facts),
            files=[f.source_path.name for f in facts],
        )

        # ---- 2. full staging before metadata is touched --------------------
        staged_entries: list[tuple[FileFacts, object, str, str]] = []
        try:
            for fact, relpath, content_fp in zip(facts, relpaths, content_fps):
                staged_path = copy_to_staging(
                    fact.source_path, self.storage.staging_dir,
                    f"{rid}__{content_fp[:12]}__{fact.source_path.name}",
                )
                staged_entries.append((fact, staged_path, relpath, content_fp))
        except ServiceError:
            # Stage failure: ledger rows for anything already staged.
            for _, staged_path, _, _ in staged_entries:
                rec_id = self.ledger.record_pending(
                    request_id=rid,
                    table_name=table_name,
                    kind=CLEANUP_KIND_STALE_STAGED,
                    path=staged_path,
                )
                remove_path(Path(staged_path), self.ledger, rec_id)
            self.store.record_terminal_failure(
                request_id=rid,
                table_name=table_name,
                operation=operation,
                base_snapshot_id=base_snapshot_id,
                request_fingerprint=payload_fp,
                category=ErrorCategory.STAGING_FAILED,
                message="one or more files failed to stage",
            )
            self.diag.indeterminate(
                rid, "commit.staging_failed", "file staging failed",
                table=table_name,
            )
            raise

        lock = self._lock_for(table_name)
        with lock:
            return self._commit_locked(
                rid=rid,
                table_name=table_name,
                operation=operation,
                base_snapshot_id=base_snapshot_id,
                spec=spec,
                staged_entries=staged_entries,
                payload_fp=payload_fp,
                schema=table_schema,
            )

    @staticmethod
    def _schema_after(
        fact: FileFacts,
        table_schema: CanonicalSchema | None,
        spec: tuple[str, ...],
    ) -> CanonicalSchema:
        if table_schema is None:
            return fact.schema
        if fact.schema != table_schema:
            raise ServiceError(
                ErrorCategory.VALIDATION,
                "parquet file schema is incompatible with the table schema",
                details={
                    "file": fact.source_path.name,
                    "incoming": fact.schema.as_dict(),
                    "expected": table_schema.as_dict(),
                },
            )
        return table_schema

    def _commit_locked(
        self,
        *,
        rid: str,
        table_name: str,
        operation: str,
        base_snapshot_id: int,
        spec: tuple[str, ...],
        staged_entries: list[tuple[FileFacts, object, str, str]],
        payload_fp: str,
        schema: CanonicalSchema | None,
    ) -> CommitResult:
        last_busy: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._attempt(
                    rid=rid,
                    table_name=table_name,
                    operation=operation,
                    base_snapshot_id=base_snapshot_id,
                    spec=spec,
                    staged_entries=staged_entries,
                    payload_fp=payload_fp,
                    schema=schema,
                    attempt=attempt,
                )
            except ServiceError as exc:
                # Write-lock contention is the only thing worth retrying;
                # conflicts and validation errors are terminal answers.
                if exc.category != ErrorCategory.CONFLICT_RETRY_EXHAUSTED:
                    self._discard_staged(rid, table_name, staged_entries,
                                         committed=False)
                    raise
                last_busy = exc
                continue
        # Retry budget exhausted while contending for the metadata lock.
        self._discard_staged(rid, table_name, staged_entries, committed=False)
        raise ServiceError(
            ErrorCategory.CONFLICT_RETRY_EXHAUSTED,
            f"metadata lock contention after {self.max_attempts} attempts",
            details={"request_id": rid, "cause": str(last_busy)},
            request_id=rid,
        )

    def _attempt(
        self,
        *,
        rid: str,
        table_name: str,
        operation: str,
        base_snapshot_id: int,
        spec: tuple[str, ...],
        staged_entries: list[tuple[FileFacts, object, str, str]],
        payload_fp: str,
        schema: CanonicalSchema | None,
        attempt: int,
    ) -> CommitResult:
        with self.store.exclusive() as tx:
            prior = tx.existing_outcome(rid, payload_fp)
            if prior is not None:
                return self._replay(prior, staged_entries)

            tx.insert_pending(
                request_id=rid,
                table_name=table_name,
                operation=operation,
                base_snapshot_id=base_snapshot_id,
                request_fingerprint=payload_fp,
                attempts=attempt,
            )

            entries = [
                NewFileEntry(
                    relpath=relpath,
                    staged_path=Path(staged_path),
                    source_name=fact.source_path.name,
                    fingerprint=content_fp,
                    row_count=fact.row_count,
                    partition_keys=partition_keys(fact.partitions),
                )
                for fact, staged_path, relpath, content_fp in staged_entries
            ]

            head_before = self.store.head_snapshot_id(table_name)
            try:
                snapshot_id, rebased = tx.plan_and_apply(
                    table_name=table_name,
                    partition_spec=spec,
                    operation=operation,
                    request_id=rid,
                    base_snapshot_id=base_snapshot_id,
                    entries=entries,
                )
            except ServiceError as exc:
                # Terminal rejection (or validation error discovered in tx).
                self._discard_staged(rid, table_name, staged_entries,
                                     committed=False)
                self.diag.rejected(
                    rid, "commit.rejected", exc.message,
                    table=table_name,
                    category=exc.category.value,
                    base_snapshot_id=base_snapshot_id,
                    head_snapshot_id=head_before,
                    details=exc.details,
                    attempt=attempt,
                )
                raise

            # ---- publish physical files while metadata tx is open ----------
            published: list[tuple[object, str]] = []
            if schema is not None and self.store.get_table(table_name)["schema_json"] is None:
                self.store.set_schema(table_name, schema.to_json())
            try:
                for entry in entries:
                    self._publish_idempotent(entry, rid)
                    published.append((entry.staged_path, entry.relpath))
            except ServiceError as exc:
                # Metadata transaction rolls back; files already moved into
                # the warehouse are unreferenced orphans — ledger them now.
                tx.mark_failed(rid, ErrorCategory.INDETERMINATE, exc.message)
                for _, relpath in published:
                    orphan_path = self.storage.warehouse_dir / relpath
                    rec_id = self.ledger.record_pending(
                        request_id=rid,
                        table_name=table_name,
                        kind=CLEANUP_KIND_PARTIAL_PUBLISH,
                        path=orphan_path,
                    )
                    remove_path(orphan_path, self.ledger, rec_id)
                # Remaining staged files are garbage too.
                for entry in entries[len(published):]:
                    rec_id = self.ledger.record_pending(
                        request_id=rid,
                        table_name=table_name,
                        kind=CLEANUP_KIND_STALE_STAGED,
                        path=entry.staged_path,
                    )
                    remove_path(Path(entry.staged_path), self.ledger, rec_id)
                self.diag.indeterminate(
                    rid, "commit.publish_failed", exc.message,
                    table=table_name,
                    published_so_far=[rp for _, rp in published],
                    attempt=attempt,
                )
                raise

        # Commit durable. Snapshot files are now referenced; nothing to clean.
        snap_entries = self.store.snapshot_entries(snapshot_id)
        total_rows = sum(e.row_count for e in snap_entries)
        touched = sorted({k for e in entries for k in e.partition_keys})
        self.diag.accepted(
            rid, "commit.committed",
            table=table_name,
            operation=operation,
            base_snapshot_id=base_snapshot_id,
            head_snapshot_id=snapshot_id,
            rebased=rebased,
            attempt=attempt,
            files=[e.relpath for e in entries],
            partition_keys=touched,
        )
        return CommitResult(
            request_id=rid,
            table_name=table_name,
            operation=operation,
            status=STATUS_COMMITTED,
            base_snapshot_id=base_snapshot_id,
            head_snapshot_id=head_before,
            snapshot_id=snapshot_id,
            rebased=rebased,
            replayed=False,
            attempts=attempt,
            files=[
                {
                    "relpath": e.relpath,
                    "source": e.source_name,
                    "fingerprint": e.fingerprint[:12],
                    "row_count": e.row_count,
                    "partition_keys": list(e.partition_keys),
                }
                for e in entries
            ],
            total_row_count=total_rows,
            partition_keys=touched,
        )

    def _publish_idempotent(self, entry: NewFileEntry, rid: str) -> None:
        """Publish one staged file; tolerate an identical file at destination.

        Deterministic relpaths make request-id replays safe: if the warehouse
        path already holds byte-identical content, the publish is a no-op.
        Different content at that path is INDETERMINATE.
        """
        dest = self.storage.warehouse_dir / entry.relpath
        if dest.exists():
            existing_fp = _sha256_file(dest)
            staged_fp = _sha256_file(entry.staged_path)
            if existing_fp == staged_fp:
                # Same content already published (crash/replay); consume the
                # staged copy and treat as success.
                Path(entry.staged_path).unlink(missing_ok=True)
                return
            raise ServiceError(
                ErrorCategory.INDETERMINATE,
                "warehouse path holds different content than staged file",
                details={
                    "relpath": entry.relpath,
                    "warehouse_fingerprint": existing_fp[:12],
                    "staged_fingerprint": staged_fp[:12],
                },
                request_id=rid,
            )
        self.storage.publish_one(
            _StagedView(entry.staged_path, rid, entry.source_name,
                        entry.fingerprint),
            entry.relpath,
        )

    def _replay(self, record, staged_entries) -> CommitResult:
        """Reconstruct the response for a previously decided request."""
        rid = record.request_id
        if record.status == STATUS_COMMITTED and record.final_snapshot_id:
            snap_entries = self.store.snapshot_entries(record.final_snapshot_id)
            rows = {e.file_relpath: e for e in snap_entries}
            files: list[dict] = []
            total_rows = sum(e.row_count for e in snap_entries)
            touched = sorted({k for e in snap_entries for k in e.partition_keys})
            for fact, _, relpath, content_fp in staged_entries:
                e = rows.get(relpath)
                files.append(
                    {
                        "relpath": relpath,
                        "source": fact.source_path.name,
                        "fingerprint": content_fp[:12],
                        "row_count": e.row_count if e else 0,
                        "partition_keys": list(e.partition_keys) if e else [],
                    }
                )
            self.diag.accepted(
                rid, "commit.replayed",
                table=record.table_name,
                snapshot_id=record.final_snapshot_id,
                prior_status=record.status,
            )
            self._discard_staged(rid, record.table_name, staged_entries,
                                 committed=True)
            return CommitResult(
                request_id=rid,
                table_name=record.table_name,
                operation=record.operation,
                status=STATUS_COMMITTED,
                base_snapshot_id=record.base_snapshot_id,
                head_snapshot_id=record.base_snapshot_id,
                snapshot_id=record.final_snapshot_id,
                rebased=bool(record.rebased),
                replayed=True,
                attempts=record.attempts,
                files=files,
                total_row_count=total_rows,
                partition_keys=touched,
            )

        # Prior rejection / failure: replay the exact category.
        category = (
            ErrorCategory(record.error_category)
            if record.error_category
            else ErrorCategory.INDETERMINATE
        )
        self._discard_staged(rid, record.table_name, staged_entries,
                             committed=False)
        self.diag.rejected(
            rid, "commit.replayed_rejection",
            record.error_message or "stored failure",
            table=record.table_name,
            category=category.value,
        )
        raise ServiceError(
            category,
            record.error_message or "request previously failed",
            details={"replayed": True},
            request_id=rid,
        )

    def _discard_staged(
        self,
        rid: str,
        table_name: str,
        staged_entries: list[tuple[FileFacts, object, str, str]],
        *,
        committed: bool,
    ) -> None:
        """Give each staged file its own cleanup-ledger row, then remove it."""
        for _, staged_path, _, _ in staged_entries:
            p = Path(staged_path)
            if not p.exists():
                continue
            kind = CLEANUP_KIND_STALE_STAGED
            rec_id = self.ledger.record_pending(
                request_id=rid, table_name=table_name, kind=kind, path=p
            )
            remove_path(p, self.ledger, rec_id)

    # ------------------------------------------------------------- recovery

    def recover_pending(self) -> list[dict]:
        """Resolve PENDING commits left by a crashed process.

        SQLite guarantees snapshot + HEAD + status update are one atomic
        transaction, so PENDING means *no* snapshot was committed.  We drop the
        pending row (client may safely retry with the same request id); staged
        files get ledger rows, and any files already published but never
        referenced surface through the warehouse orphan scan.
        """
        resolved: list[dict] = []
        with self.store.exclusive() as tx:
            pending = self.store.pending_commits()
            for rec in pending:
                self._conn_update_pending_failed(rec.request_id)
                resolved.append(
                    {"request_id": rec.request_id, "table": rec.table_name}
                )
        for item in resolved:
            rid = item["request_id"]
            for staged in self.storage.staging_dir.glob(f"{rid}__*"):
                rec_id = self.ledger.record_pending(
                    request_id=rid,
                    table_name=item["table"],
                    kind=CLEANUP_KIND_STALE_STAGED,
                    path=staged,
                )
                remove_path(staged, self.ledger, rec_id)
            self.diag.indeterminate(
                rid, "recover.pending_cleared",
                "interrupted commit cleared; retry with the same request_id",
                table=item["table"],
            )
        return resolved

    def _conn_update_pending_failed(self, request_id: str) -> None:
        conn = self.store._conn  # noqa: SLF001 - package-internal protocol
        conn.execute(
            "DELETE FROM commits WHERE request_id = ? AND status = ?",
            (request_id, _META_STATUS_PENDING),
        )

    # ------------------------------------------------------------- orphans

    def scan_orphans(self, table_name: str | None = None) -> dict:
        """List physical warehouse files/dirs that no snapshot references."""
        tables = [table_name] if table_name else self._all_table_names()
        referenced: set[str] = set()
        for t in tables:
            referenced |= self.store.all_referenced_relpaths(t)

        physical = self._all_warehouse_relpaths()
        orphan_files = sorted(p for p in physical["files"] if p not in referenced)
        # A directory is orphan when it contains no referenced file at all and
        # sits under a known table (or is a top-level non-table directory).
        orphan_dirs = self._orphan_dirs(tables, referenced, physical)
        return {
            "orphan_files": orphan_files,
            "orphan_directories": orphan_dirs,
            "referenced_file_count": len(referenced),
        }

    def reconcile_orphans(self, table_name: str | None = None) -> dict:
        scan = self.scan_orphans(table_name)
        removed_files: list[str] = []
        for rel in scan["orphan_files"]:
            p = self.storage.warehouse_dir / rel
            rec_id = self.ledger.record_pending(
                request_id="req-reconcile",
                table_name=table_name or "_warehouse",
                kind=CLEANUP_KIND_WAREHOUSE_ORPHAN,
                path=p,
            )
            if remove_path(p, self.ledger, rec_id):
                removed_files.append(rel)
        removed_dirs: list[str] = []
        for rel in scan["orphan_directories"]:
            p = self.storage.warehouse_dir / rel
            if p.exists():
                rec_id = self.ledger.record_pending(
                    request_id="req-reconcile",
                    table_name=table_name or "_warehouse",
                    kind=CLEANUP_KIND_WAREHOUSE_ORPHAN,
                    path=p,
                )
                if remove_path(p, self.ledger, rec_id):
                    removed_dirs.append(rel)
        self.diag.emit(
            request_id="req-reconcile",
            event="orphan.reconciled",
            decision="INFO",
            removed_files=removed_files,
            removed_directories=removed_dirs,
        )
        return {
            "removed_files": removed_files,
            "removed_directories": removed_dirs,
            "scan": scan,
        }

    def _all_table_names(self) -> list[str]:
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT table_name FROM tables ORDER BY table_name"
            ).fetchall()
        return [r[0] for r in rows]

    def _all_warehouse_relpaths(self) -> dict:
        root = self.storage.warehouse_dir
        files: list[str] = []
        dirs: list[str] = []
        for path in root.rglob("*"):
            rel = str(path.relative_to(root))
            if path.is_file():
                files.append(rel)
            elif path.is_dir():
                dirs.append(rel)
        return {"files": files, "dirs": sorted(dirs)}

    def _orphan_dirs(
        self, tables: list[str], referenced: set[str], physical: dict
    ) -> list[str]:
        orphan: list[str] = []
        known = set(tables)
        for d in physical["dirs"]:
            top = d.split("/", 1)[0]
            if top not in known:
                # Entire top-level directory is unrelated to any table.
                orphan.append(d)
                continue
            # Directory under a table: orphan if no referenced file lives
            # inside it.
            prefix = d.rstrip("/") + "/"
            if not any(r.startswith(prefix) for r in referenced):
                orphan.append(d)
        # Report only deepest (most specific) orphan dirs, plus empty leaves.
        orphan.sort()
        return [d for d in orphan if not _has_orphan_ancestor(d, orphan)]


def _has_orphan_ancestor(d: str, all_orphans: list[str]) -> bool:
    for other in all_orphans:
        if other != d and d.startswith(other.rstrip("/") + "/"):
            return True
    return False


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_file_or_missing(path: Path) -> str:
    """Content hash for any readable file; stable missing-marker otherwise."""
    try:
        return _sha256_file(path)
    except OSError:
        return "missing:" + hashlib.sha256(
            json.dumps({"name": path.name}, sort_keys=True).encode()
        ).hexdigest()


def _payload_fingerprint(
    table_name: str,
    operation: str,
    base_snapshot_id: int,
    content_fps: Sequence[str],
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "table": table_name,
                "operation": operation,
                "base": base_snapshot_id,
                "files": sorted(content_fps),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]


@dataclass(frozen=True)
class _StagedView:
    """Minimal staged-file view FileStorage.publish_one expects."""

    staged_path: Path
    request_id: str
    source_name: str
    fingerprint: str
