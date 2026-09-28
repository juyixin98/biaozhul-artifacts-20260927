"""编排引擎：把适配、配置、快照、决策、验证、提交与日志串成一次完整运行。

阶段顺序（也是错误优先级，阶段内冲突一次性收集）：

  1. run_started / 适配读源          -> SOURCE_FORMAT_ERROR
  2. 目标表引导（仅 commit）         -> SCHEMA_MISMATCH（键列缺失）
  3. 配置解析（含条件树校验）        -> CONFIG_INVALID
  4. 源行数/字节资源上限（早期）     -> PLAN_TOO_LARGE
  5. 装载操作前快照
  6. planner 决策：
       源重复键 -> SOURCE_DUPLICATE_KEY
       源 NULL 键（SQL）-> KEY_NULL_REJECTED
       目标重复键 -> TARGET_DUPLICATE_KEY
  7. 计划验证：动作数/字节数、可序列化 -> PLAN_TOO_LARGE / COMPUTATION_FAILURE
  8. commit：单 IMMEDIATE 事务        -> COMMIT_FAILED / DISK_FULL（回滚）
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import store
from .adapter import SourceBatch, load_source
from .config import build_spec
from .contracts import MergePlan, RunResult
from .errors import (
    CommitFailure,
    DiskFullError,
    MergeError,
    SchemaMismatchError,
)
from .journal import Journal
from .planner import plan_debug_summary, plan_merge
from .snapshot import key_jsonable, row_key
from .validator import assert_json_serializable, check_source_limits, validate_plan


@dataclass(frozen=True)
class MergeRequest:
    source: dict[str, Any]
    config: dict[str, Any]
    dry_run: bool = False
    fault_point: str | None = None      # after_actions | before_commit | commit_raises


class MergeEngine:
    def __init__(
        self,
        db_path: str | Path,
        journal_dir: str | Path,
        *,
        clock=time.time,
        id_factory=lambda: uuid.uuid4().hex[:12],
    ) -> None:
        self.db_path = str(db_path)
        self.journal = Journal(journal_dir, clock=clock)
        self._clock = clock
        self._id_factory = id_factory

    # ---- 对外主入口 --------------------------------------------------------

    def run(self, request: MergeRequest) -> RunResult:
        run_id = self._id_factory()
        started_at = self._clock()
        j = self.journal
        table = str(request.config.get("target_table", "?"))

        j.event(run_id, "start", "run_started",
                reason="merge run accepted",
                dry_run=request.dry_run,
                target_table=table,
                fault_point=request.fault_point,
                source_format=request.source.get("format"))

        conn = store.connect(self.db_path)
        try:
            store.ensure_meta(conn)

            # 1) 适配
            try:
                key_columns_hint = tuple(request.config.get("key_columns", []))
                batch = load_source(request.source, key_columns=key_columns_hint)
            except MergeError as exc:
                return self._reject(conn, run_id, started_at, request, j, exc,
                                    phase="adapter")
            j.event(run_id, "adapter", "source_loaded",
                    reason=f"loaded {len(batch.rows)} rows from {batch.format}",
                    format=batch.format, rows=len(batch.rows),
                    columns=list(batch.columns), total_bytes=batch.total_bytes,
                    sample_keys=self._sample_source_keys(batch, key_columns_hint))

            # 2) 目标结构：commit 时确保列存在（可建表/加列）；
            #    dry-run 只读现有列，绝不建表或改表
            table_name = request.config.get("target_table")
            if not request.dry_run:
                try:
                    target_columns = tuple(self._bootstrap(conn, request.config, batch))
                except MergeError as exc:
                    return self._reject(conn, run_id, started_at, request, j, exc,
                                        phase="schema")
            else:
                existing = (store.table_columns(conn, table_name)
                            if isinstance(table_name, str) else [])
                target_columns = tuple(existing)

            # 3) 配置解析（条件列校验使用源列 ∪ 目标列）
            try:
                spec = build_spec(request.config, batch.columns, target_columns)
            except MergeError as exc:
                return self._reject(conn, run_id, started_at, request, j, exc,
                                    phase="config")
            j.event(run_id, "config", "null_policy",
                    reason=f"null equality = {spec.null_equality.value}",
                    null_equality=spec.null_equality.value,
                    delete_unmatched=spec.delete_unmatched)

            # 4) 早期资源检查
            try:
                check_source_limits(len(batch.rows), batch.total_bytes, spec)
            except MergeError as exc:
                return self._reject(conn, run_id, started_at, request, j, exc,
                                    phase="validate_source")

            # 5) 操作前快照
            target_rows = store.load_snapshot(conn, spec.target_table, spec.key_columns)
            j.event(run_id, "snapshot", "snapshot_loaded",
                    reason=f"pre-image snapshot with {len(target_rows)} rows",
                    target_rows=len(target_rows),
                    snapshot_rowids=[rid for rid, _ in target_rows])

            # 6) 决策（内部顺序：源重复键 -> NULL 策略 -> 目标重复键 -> 逐行）
            try:
                plan = plan_merge(batch, spec, target_rows)
            except MergeError as exc:
                return self._reject(conn, run_id, started_at, request, j, exc,
                                    phase="plan")
            summary = plan_debug_summary(plan)
            j.event(run_id, "plan", "plan_decided",
                    reason="plan computed from pre-image snapshot only",
                    fingerprint=plan.snapshot_fingerprint,
                    write_counts=plan.write_counts,
                    actions=summary["actions"])

            # 7) 计划验证
            try:
                stats = validate_plan(plan, spec)
                assert_json_serializable(plan)
            except MergeError as exc:
                return self._reject(conn, run_id, started_at, request, j, exc,
                                    phase="validate_plan")
            j.event(run_id, "validate", "plan_validated",
                    reason="all actions validated before any commit",
                    source_rows=stats.source_rows,
                    write_actions=stats.write_actions,
                    plan_bytes=stats.plan_bytes, limits=stats.limits)

            # 8) dry-run：到此为止，绝不写目标表
            if request.dry_run:
                store.record_run(
                    conn, run_id=run_id, created_at=started_at, dry_run=True,
                    status="PLANNED", target_table=spec.target_table,
                    fingerprint=plan.snapshot_fingerprint,
                    counts=plan.write_counts, error=None,
                )
                j.event(run_id, "done", "dry_run_done",
                        reason="dry-run: no target writes performed")
                return RunResult(run_id=run_id, dry_run=True, status="PLANNED",
                                 target_table=spec.target_table, plan=plan,
                                 counts=plan.write_counts)

            # 9) 原子提交
            j.event(run_id, "commit", "commit_started",
                    reason=f"applying {len(plan.write_actions())} write actions in one "
                           "IMMEDIATE transaction",
                    fault_point=request.fault_point)
            try:
                store.apply_plan(
                    conn, plan, run_id, started_at,
                    hooks=store.FaultHooks(request.fault_point),
                )
            except OSError as exc:
                # ENOSPC 等操作系统级容量错误 -> RESOURCE_EXHAUSTED
                mapped: MergeError
                if getattr(exc, "errno", None) == 28:
                    mapped = DiskFullError(str(exc), details={"errno": exc.errno})
                else:
                    mapped = CommitFailure(str(exc))
                return self._fail(conn, run_id, started_at, request, j, mapped,
                                  plan=plan)
            except sqlite3.Error as exc:
                mapped = CommitFailure(
                    f"sqlite commit failed: {exc}",
                    details={"sqlite_error": exc.__class__.__name__})
                return self._fail(conn, run_id, started_at, request, j, mapped,
                                  plan=plan)
            except RuntimeError as exc:
                # 注入点 after_actions 或 rowcount 不变量破裂都走这里
                mapped = CommitFailure(str(exc))
                return self._fail(conn, run_id, started_at, request, j, mapped,
                                  plan=plan)

            j.event(run_id, "done", "run_committed",
                    reason="all actions committed atomically",
                    counts=plan.write_counts)
            return RunResult(run_id=run_id, dry_run=False, status="COMMITTED",
                             target_table=spec.target_table, plan=plan,
                             counts=plan.write_counts)
        finally:
            conn.close()

    # ---- 查询辅助 ----------------------------------------------------------

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        conn = store.connect(self.db_path)
        try:
            store.ensure_meta(conn)
            return store.get_run(conn, run_id)
        finally:
            conn.close()

    def get_actions(self, run_id: str) -> list[dict[str, Any]]:
        conn = store.connect(self.db_path)
        try:
            store.ensure_meta(conn)
            return store.list_actions(conn, run_id)
        finally:
            conn.close()

    def get_target_rows(self, table: str) -> list[dict[str, Any]]:
        conn = store.connect(self.db_path)
        try:
            store.ensure_meta(conn)
            rows = store.load_snapshot(conn, table, ())
            return [{"rowid": rid, **vals} for rid, vals in rows]
        finally:
            conn.close()

    # ---- 内部 --------------------------------------------------------------

    def _bootstrap(self, conn, raw_config: dict[str, Any], batch: SourceBatch):
        table = raw_config.get("target_table")
        keys = tuple(raw_config.get("key_columns") or ())
        existing = store.table_columns(conn, table) if isinstance(table, str) else []
        if existing:
            missing = [k for k in keys if k not in existing]
            if missing:
                raise SchemaMismatchError(
                    f"existing target table {table!r} is missing key column(s): {missing}",
                    details={"table": table, "missing": missing,
                             "existing_columns": existing},
                )
            payload = [c for c in batch.columns if c not in keys]
            return store.bootstrap_target(conn, table, keys, tuple(payload))
        # 表不存在：按本批列建表
        payload = tuple(c for c in batch.columns if c not in keys)
        return store.bootstrap_target(conn, table, keys, payload)

    def _reject(self, conn, run_id, started_at, request, journal, exc: MergeError,
                *, phase: str) -> RunResult:
        """决策/验证期拒绝：不开启数据事务，记录 REJECTED 后返回。"""
        journal.event(run_id, phase, "run_rejected",
                      reason=f"rejected: [{exc.code}] {exc.args[0] and exc.args[0]}",
                      error=exc.to_dict())
        self._safe_record(conn, run_id, started_at, request, "REJECTED", exc,
                          fingerprint=None, counts=None)
        return RunResult(run_id=run_id, dry_run=request.dry_run, status="REJECTED",
                         target_table=str(request.config.get("target_table", "?")),
                         plan=None, counts={}, error=exc.to_dict())

    def _fail(self, conn, run_id, started_at, request, journal, exc: MergeError,
              *, plan: MergePlan | None) -> RunResult:
        """提交期失败：apply_plan 已 ROLLBACK，记录 FAILED。"""
        fingerprint = plan.snapshot_fingerprint if plan else None
        counts = plan.write_counts if plan else None
        journal.event(run_id, "commit", "commit_failed",
                      reason=f"commit failed: [{exc.code}] {exc.args[0] and exc.args[0]}; "
                             "transaction rolled back, no partial update",
                      error=exc.to_dict(),
                      snapshot_fingerprint=fingerprint)
        self._safe_record(conn, run_id, started_at, request, "FAILED", exc,
                          fingerprint=fingerprint, counts=counts)
        return RunResult(run_id=run_id, dry_run=False, status="FAILED",
                         target_table=str(request.config.get("target_table", "?")),
                         plan=plan, counts={}, error=exc.to_dict())

    def _safe_record(self, conn, run_id, started_at, request, status, exc,
                     *, fingerprint, counts) -> None:
        try:
            store.record_run(
                conn, run_id=run_id, created_at=started_at,
                dry_run=request.dry_run, status=status,
                target_table=str(request.config.get("target_table", "?")),
                fingerprint=fingerprint, counts=counts, error=exc.to_dict(),
            )
        except sqlite3.Error as meta_exc:  # 元数据事务自身失败属于计算失败
            raise CommitFailure(
                f"failed to persist run metadata: {meta_exc}",
                details={"original_error": exc.code},
            ) from meta_exc

    @staticmethod
    def _sample_source_keys(batch: SourceBatch, key_columns: tuple[str, ...],
                            limit: int = 5) -> list[Any]:
        if not key_columns:
            return []
        return [
            key_jsonable(row_key(batch.rows[i].values, key_columns))
            for i in range(min(limit, len(batch.rows)))
        ]
