"""Orchestration facade tying adapter, snapshot, planner, executor together.

Transaction design
------------------
The same SQLite file holds target data and merge metadata, but two separate
transaction scopes are used:

1. **Planning connection** (``connect()``) reads the snapshot.
2. **Execution connection** (``connect()``) opens ``BEGIN IMMEDIATE`` for data
   changes only. Metadata rows are written outside that transaction, before
   execution (run/actions/traces/snapshots) and after (terminal status), so a
   failed merge leaves a complete audit trail while data rolls back cleanly.

Validate and merge share a single pipeline up to the plan; validate stops
there, merge continues into the executor. Both produce a run id with full
metadata + JSONL trail.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import metadata as meta
from .adapter import load_source_rows
from .contract import MergePlan, MergeSpec, NullPolicy, as_public_dict
from .errors import MergeError
from .executor import execute_plan
from .planner import build_plan, prepare
from .runlog import JsonlRunLogger, actions_payload, decisions_payload
from .snapshot import find_duplicate_keys, snapshot_target, table_columns


def connect(
    db_path: str,
    *,
    timeout: float = 5.0,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open a connection with fixed, deterministic SQLite PRAGMAs.

    ``check_same_thread=False`` is appropriate only with external request
    serialization (the in-process TestClient drives the app from another
    thread but serializes requests).
    """
    conn = sqlite3.connect(
        db_path, timeout=timeout, check_same_thread=check_same_thread
    )
    conn.row_factory = None
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


class MergeService:
    """Stateful facade bound to a database file and run-log path."""

    def __context__(self) -> "MergeService":
        return self

    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        log_path: str = "logs/merge-runs.jsonl",
        timeout: float = 5.0,
        check_same_thread: bool = True,
    ) -> None:
        self.db_path = db_path
        self.timeout = timeout
        self.conn = connect(
            db_path, timeout=timeout, check_same_thread=check_same_thread
        )
        meta.init_metadata(self.conn)
        self.logger = JsonlRunLogger(log_path)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ reads
    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return meta.list_runs(self.conn, limit=limit)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return meta.get_run(self.conn, run_id)

    def get_actions(self, run_id: str) -> list[dict[str, Any]]:
        return meta.get_actions(self.conn, run_id)

    def get_traces(self, run_id: str) -> list[dict[str, Any]]:
        return meta.get_traces(self.conn, run_id)

    def get_snapshot(self, run_id: str) -> dict[str, Any] | None:
        return meta.get_snapshots(self.conn, run_id)

    # ---------------------------------------------------------------- helpers
    def _allocate_run(self) -> tuple[str, int, str]:
        run_id, seq = meta.new_run_id(self.conn)
        return run_id, seq, datetime.now(timezone.utc).isoformat()

    def _not_null_columns(self, table: str) -> frozenset[str]:
        """NOT NULL columns that have no database default.

        Columns with a DEFAULT can be omitted from INSERT mappings safely; only
        this narrower set must be supplied (and non-NULL) by the source.
        """
        cur = self.conn.execute(
            "SELECT name FROM pragma_table_info(?) "
            'WHERE "notnull" = 1 AND dflt_value IS NULL',
            (table,),
        )
        return frozenset(r[0] for r in cur.fetchall())

    def _run_failure(self, run_id: str, seq: int, error: MergeError, stage: str) -> None:
        error._run_id = run_id  # type: ignore[attr-defined]
        try:
            meta.finish_run(self.conn, run_id=run_id, status="failed", error=error)
            self.conn.commit()
        finally:
            self.logger.failed(run_id=run_id, seq=seq, error=error, stage=stage)

    def _run_reject(self, run_id: str, seq: int, error: MergeError, stage: str) -> None:
        error._run_id = run_id  # type: ignore[attr-defined]
        try:
            meta.finish_run(self.conn, run_id=run_id, status="rejected", error=error)
            self.conn.commit()
        finally:
            self.logger.rejected(run_id=run_id, seq=seq, error=error, stage=stage)

    # ---------------------------------------------------------------- validate
    def validate(self, request: dict[str, Any]) -> dict[str, Any]:
        """Build and return the plan without executing any data change."""
        run_id, seq, started = self._allocate_run()
        try:
            spec = MergeSpec.from_payload(request.get("merge") or request)
        except MergeError as exc:
            exc._run_id = run_id  # type: ignore[attr-defined]
            self._record_early_reject(run_id, seq, started, request, exc)
            raise

        self.logger.run_start(
            run_id=run_id,
            seq=seq,
            spec=as_public_dict(spec),
            source_rows=request.get("source", {}).get("records"),
            source_fingerprint="pending",
        )
        plan = self._plan(run_id, seq, request, spec, execute=False)
        return self._plan_response(run_id, seq, plan, executed=False)

    # ------------------------------------------------------------------- merge
    def merge(
        self,
        request: dict[str, Any],
        *,
        failpoint: str | None = None,
    ) -> dict[str, Any]:
        """Validate then atomically commit; any failure rolls data back."""
        run_id, seq, started = self._allocate_run()
        try:
            spec = MergeSpec.from_payload(request.get("merge") or request)
        except MergeError as exc:
            exc._run_id = run_id  # type: ignore[attr-defined]
            self._record_early_reject(run_id, seq, started, request, exc)
            raise

        self.logger.run_start(
            run_id=run_id,
            seq=seq,
            spec=as_public_dict(spec),
            source_rows=request.get("source", {}).get("records"),
            source_fingerprint="pending",
        )

        plan = self._plan(run_id, seq, request, spec, execute=True)

        if failpoint:
            self.logger.fault(run_id, seq, failpoint, phase="before_commit")

        try:
            counts = execute_plan(self.conn, plan, failpoint=failpoint)
        except MergeError as exc:
            self._run_failure(run_id, seq, exc, stage="execute")
            raise

        meta.finish_run(self.conn, run_id=run_id, status="committed")
        self.conn.commit()
        self.logger.committed(run_id=run_id, seq=seq, counts=counts)
        return {
            **self._plan_response(run_id, seq, plan, executed=True),
            "committed": True,
            "committed_counts": counts,
        }

    # -------------------------------------------------------------- internals
    def _record_early_reject(
        self,
        run_id: str,
        seq: int,
        started: str,
        request: dict[str, Any],
        exc: MergeError,
    ) -> None:
        merge_part = request.get("merge") if isinstance(request, dict) else None
        table = ""
        policy = NullPolicy.SQL_NOT_DISTINCT.value
        if isinstance(merge_part, dict):
            table = str(merge_part.get("target_table") or "")
            policy = str(merge_part.get("null_policy") or policy)
        try:
            meta.insert_early_rejected_run(
                self.conn,
                run_id=run_id,
                seq=seq,
                started_at=started,
                target_table=table,
                null_policy=policy,
                raw_spec=merge_part or request,
                error=exc,
            )
            self.conn.commit()
        finally:
            self.logger.rejected(run_id=run_id, seq=seq, error=exc, stage="spec")

    def _plan(
        self,
        run_id: str,
        seq: int,
        request: dict[str, Any],
        spec: MergeSpec,
        *,
        execute: bool,
    ) -> MergePlan:
        # 1) adapter
        try:
            source_rows, source_fp = load_source_rows(
                request.get("source"), max_rows=spec.max_source_rows
            )
        except MergeError as exc:
            self._record_plan_stage_reject(run_id, seq, spec, exc, stage="adapter")
            raise

        # 2) empty batch fast path: no source schema exists to bind
        #    expressions against, so an empty source is a contract-level
        #    no-op. The target table must still exist (missing -> conflict).
        if not source_rows:
            return self._build_empty_plan(run_id, seq, request, spec, execute)

        # 3) provisional compile to discover which target columns the snapshot
        #    must contain; target schema existence checked via table_columns.
        try:
            target_columns = table_columns(self.conn, spec.target_table)
        except MergeError as exc:
            self._record_plan_stage_reject(run_id, seq, spec, exc, stage="snapshot")
            raise

        # 4) prepare (column contract + expression compilation)
        try:
            prepared = prepare(
                spec,
                source_columns=sorted({c for r in source_rows for c in r.values}),
                target_columns=target_columns,
                not_null_target_columns=self._not_null_columns(spec.target_table),
            )
        except MergeError as exc:
            self._record_plan_stage_reject(run_id, seq, spec, exc, stage="prepare")
            raise

        needed = sorted(set(prepared.needed_target_columns()) | set(spec.key_columns))
        try:
            target_rows, _cols, target_fp = snapshot_target(self.conn, spec, needed)
        except MergeError as exc:
            self._record_plan_stage_reject(run_id, seq, spec, exc, stage="snapshot")
            raise

        dup_groups = find_duplicate_keys(target_rows, spec)
        self.logger.snapshot(
            run_id=run_id,
            seq=seq,
            target_rows=[
                {"rowid": r.rowid, **r.values} for r in target_rows
            ],
            target_fingerprint=target_fp,
            duplicate_groups=dup_groups,
        )

        # 4) pure decision core (target dup, source dup, expressions)
        try:
            plan = build_plan(
                prepared,
                source_rows,
                target_rows,
                source_fingerprint=source_fp,
                target_fingerprint=target_fp,
            )
        except MergeError as exc:
            self._record_plan_stage_reject(run_id, seq, spec, exc, stage="planner")
            raise

        # 5) persist the full replay bundle (metadata tx, never the data tx)
        source_payload = [{"index": r.index, **r.values} for r in source_rows]
        target_payload = [{"rowid": r.rowid, **r.values} for r in target_rows]
        meta.insert_run(
            self.conn, run_id=run_id, seq=seq, plan=plan, started_at=self._started(run_id)
        )
        meta.insert_plan_payload(
            self.conn,
            run_id=run_id,
            plan=plan,
            source_payload=source_payload,
            target_payload=target_payload,
        )
        meta.finish_run(
            self.conn,
            run_id=run_id,
            status="planned",
        )
        self.conn.commit()

        self.logger.plan(
            run_id=run_id,
            seq=seq,
            plan=plan,
            actions_payload=actions_payload(plan),
            decisions_payload=decisions_payload(plan),
        )
        if not execute:
            self.logger.stage(
                run_id,
                seq,
                "VALIDATE_ONLY",
                source_fingerprint=source_fp,
                target_fingerprint=target_fp,
            )
        return plan

    def _started(self, run_id: str) -> str:
        cur = self.conn.execute(
            "SELECT started_at FROM merge_runs WHERE run_id = ?", (run_id,)
        )
        row = cur.fetchone()
        return row[0] if row else datetime.now(timezone.utc).isoformat()

    def _build_empty_plan(
        self,
        run_id: str,
        seq: int,
        request: dict[str, Any],
        spec: MergeSpec,
        execute: bool,
    ) -> MergePlan:
        """Empty source batch: validate target existence, persist an empty plan."""
        try:
            target_columns = table_columns(self.conn, spec.target_table)
        except MergeError as exc:
            self._record_plan_stage_reject(run_id, seq, spec, exc, stage="snapshot")
            raise
        try:
            target_rows, _cols, target_fp = snapshot_target(
                self.conn, spec, spec.key_columns
            )
        except MergeError as exc:
            self._record_plan_stage_reject(run_id, seq, spec, exc, stage="snapshot")
            raise
        dup_groups = find_duplicate_keys(target_rows, spec)
        self.logger.snapshot(
            run_id=run_id,
            seq=seq,
            target_rows=[{"rowid": r.rowid, **r.values} for r in target_rows],
            target_fingerprint=target_fp,
            duplicate_groups=dup_groups,
        )
        from .utils import fingerprint

        source_fp = fingerprint([])
        plan = MergePlan(
            spec=spec,
            actions=[],
            decisions=[],
            source_count=0,
            target_count=len(target_rows),
            source_fingerprint=source_fp,
            target_fingerprint=target_fp,
            target_rows=list(target_rows),
        )
        meta.insert_run(
            self.conn, run_id=run_id, seq=seq, plan=plan,
            started_at=self._started(run_id),
        )
        meta.insert_plan_payload(
            self.conn, run_id=run_id, plan=plan,
            source_payload=[],
            target_payload=[{"rowid": r.rowid, **r.values} for r in target_rows],
        )
        meta.finish_run(self.conn, run_id=run_id, status="planned")
        self.conn.commit()
        self.logger.plan(
            run_id=run_id, seq=seq, plan=plan,
            actions_payload=[], decisions_payload=[],
        )
        if not execute:
            self.logger.stage(run_id, seq, "VALIDATE_ONLY",
                             source_fingerprint=source_fp, target_fingerprint=target_fp)
        return plan

    def _record_plan_stage_reject(
        self, run_id: str, seq: int, spec: MergeSpec, exc: MergeError, *, stage: str
    ) -> None:
        # No run row exists yet for mid-pipeline rejects; write a minimal one so
        # the run id is queryable end to end.
        exc._run_id = run_id  # type: ignore[attr-defined]
        meta.insert_early_rejected_run(
            self.conn,
            run_id=run_id,
            seq=seq,
            started_at=datetime.now(timezone.utc).isoformat(),
            target_table=spec.target_table,
            null_policy=spec.null_policy.value,
            raw_spec=as_public_dict(spec),
            error=exc,
        )
        self.conn.commit()
        self.logger.rejected(run_id=run_id, seq=seq, error=exc, stage=stage)

    def _plan_response(
        self, run_id: str, seq: int, plan: MergePlan, *, executed: bool
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "run_seq": seq,
            "status": "committed" if executed else "planned",
            "summary": plan.summary(),
            "actions": actions_payload(plan),
            "decisions": decisions_payload(plan),
        }
