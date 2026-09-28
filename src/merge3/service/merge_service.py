"""合并应用服务：把存储、适配、内核、日志组成可核验的用例。

关键保证：
- 快照只增不改：write_snapshot 先规范化+内容寻址，同内容复用同一快照；
- 分支只能条件前进，不能回退、不能被重新导入覆盖；
- 冲突解决方案与开启合并时的三方快照身份绑定，快照换了必须重开合并；
- 提交在单个 IMMEDIATE 事务中完成（新快照 + 两条父引用 + 分支前进 + run 落库）。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from ..adapters import arrow_format
from ..config import StorageConfig
from ..domain.models import (
    Branch,
    ConflictRecord,
    FieldOrigin,
    MergeRun,
    MergeStatus,
    ResolutionKind,
    Snapshot,
    TableSpec,
)
from ..errors import (
    BindingMismatchError,
    ConflictError,
    MergeAlreadyCommittedError,
    NotFoundError,
    ResolutionRejectedError,
    UnresolvedConflictError,
    ValidationError,
)
from ..kernel import diff3, lineage
from ..storage.run_log import RunLogger, utc_now_iso
from ..storage.sqlite_store import SqliteStore


class MergeService:
    def __init__(self, cfg: StorageConfig, log_echo: bool = False):
        self.cfg = cfg
        cfg.root_dir.mkdir(parents=True, exist_ok=True)
        cfg.parquet_dir.mkdir(parents=True, exist_ok=True)
        self.store = SqliteStore(cfg.db_path)
        self.log_echo = log_echo

    # ============================================================== 表

    def register_table(self, name: str, primary_key: list[str],
                       fields: list[dict[str, Any]]) -> TableSpec:
        if self.store.get_table_spec(name) is not None:
            raise ConflictError(f"表 {name} 已存在；表结构不可变")
        spec = TableSpec(
            name=name,
            primary_key=list(primary_key),
            fields=[_field(f) for f in fields],
        )
        # 构造一次 schema 提前暴露类型/主键错误
        arrow_format.build_arrow_schema(spec)
        self.store.create_table(spec, utc_now_iso())
        return spec

    def get_spec(self, table: str) -> TableSpec:
        spec = self.store.get_table_spec(table)
        if spec is None:
            raise NotFoundError(f"表不存在: {table}")
        return spec

    # ============================================================== 快照

    def write_snapshot(
        self,
        table: str,
        rows: list[dict[str, Any]],
        parent_snapshot_id: str | None = None,
        run_id: str | None = None,
        snapshot_id_hint: str | None = None,
    ) -> Snapshot:
        """规范化、校验、内容寻址并写入不可变快照。同内容返回已存在快照。"""
        spec = self.get_spec(table)
        pa_table, count = arrow_format.rows_to_table(rows, spec)
        canonical, content_hash, _ = arrow_format.canonical_content(spec, rows)

        # 同内容幂等：同一 (table, content_hash) 直接复用，绝不产生重复历史
        for existing in self.store.list_snapshots(table):
            if existing.content_hash == content_hash:
                return existing

        snapshot_id = snapshot_id_hint or f"snap_{uuid.uuid4().hex[:16]}"
        if self.store.get_snapshot(snapshot_id) is not None:
            raise ConflictError(f"snapshot_id 已存在: {snapshot_id}")

        path = self._parquet_path(snapshot_id)
        arrow_format.write_parquet(pa_table, path)
        # 再读回校验文件可读且行数一致
        verify = arrow_format.read_parquet(path)
        if verify.num_rows != count:
            raise RuntimeError(f"快照 {snapshot_id} 写入校验失败: 行数不一致")

        snap = Snapshot(
            snapshot_id=snapshot_id,
            table=table,
            schema_version=1,
            content_hash=content_hash,
            row_count=count,
            parent_snapshot_id=parent_snapshot_id,
            created_by_run_id=run_id,
            created_at=utc_now_iso(),
        )
        parents = [parent_snapshot_id] if parent_snapshot_id else []
        self.store.insert_snapshot(snap, parents)
        return snap

    def read_snapshot_rows(self, snapshot_id: str) -> tuple[TableSpec, list[dict[str, Any]]]:
        snap = self.store.get_snapshot(snapshot_id)
        if snap is None:
            raise NotFoundError(f"快照不存在: {snapshot_id}")
        spec = self.get_spec(snap.table)
        table = arrow_format.read_parquet(self._parquet_path(snapshot_id))
        return spec, arrow_format.table_to_rows(table, spec)

    def _parquet_path(self, snapshot_id: str) -> Path:
        return self.cfg.parquet_dir / f"{snapshot_id}.parquet"

    # ============================================================== 分支

    def create_branch(self, table: str, name: str, head_snapshot_id: str) -> Branch:
        self.get_spec(table)
        if self.store.get_snapshot(head_snapshot_id) is None:
            raise NotFoundError(f"快照不存在: {head_snapshot_id}")
        if self.store.get_branch(name, table) is not None:
            raise ConflictError(f"分支已存在: {name}@{table}")
        branch = Branch(name=name, table=table, head_snapshot_id=head_snapshot_id)
        self.store.create_branch(branch)
        return branch

    def get_branch_head(self, table: str, name: str) -> Snapshot:
        branch = self.store.get_branch(name, table)
        if branch is None:
            raise NotFoundError(f"分支不存在: {name}@{table}")
        snap = self.store.get_snapshot(branch.head_snapshot_id)
        assert snap is not None
        return snap

    def commit_rows(
        self, table: str, branch: str, rows: list[dict[str, Any]],
        message: str | None = None,
    ) -> Snapshot:
        """普通开发提交：在分支头上线性追加一个不可变快照并条件前进分支。"""
        head = self.get_branch_head(table, branch)
        snap = self.write_snapshot(table, rows, parent_snapshot_id=head.snapshot_id)
        self._advance_or_raise(branch, table, head.snapshot_id, snap.snapshot_id)
        return snap

    def _advance_or_raise(self, branch: str, table: str,
                          old_head: str, new_head: str) -> None:
        with self.store.transaction() as conn:
            if not self.store.advance_branch(conn, branch, table, old_head, new_head):
                current = self.store.get_branch(branch, table)
                cur_head = current.head_snapshot_id if current else "?"
                raise ConflictError(
                    f"分支 {branch}@{table} 已从 {old_head[:12]} 前进到 {cur_head[:12]}；"
                    "本次提交基于过期指针，已拒绝（禁止覆盖分支历史）"
                )

    # ============================================================== 合并：开启

    def start_merge(
        self,
        table: str,
        ours_branch: str = "develop",
        theirs_branch: str = "main",
        base_snapshot_id: str | None = None,
        run_id_hint: str | None = None,
    ) -> MergeRun:
        spec = self.get_spec(table)
        ours_head = self.get_branch_head(table, ours_branch)
        theirs_head = self.get_branch_head(table, theirs_branch)

        if base_snapshot_id is None:
            base_id = lineage.find_lca(
                ours_head.snapshot_id, theirs_head.snapshot_id,
                self.store.parent_map(table),
            )
        else:
            if self.store.get_snapshot(base_snapshot_id) is None:
                raise NotFoundError(f"指定的共同祖先快照不存在: {base_snapshot_id}")
            base_id = base_snapshot_id

        run_id = run_id_hint or f"run_{uuid.uuid4().hex[:16]}"
        log = self._logger(run_id)
        log.start(table, base_id, ours_head.snapshot_id, theirs_head.snapshot_id,
                  ours_branch, theirs_branch)

        spec_b, base_rows = self.read_snapshot_rows(base_id)
        _, ours_rows = self.read_snapshot_rows(ours_head.snapshot_id)
        _, theirs_rows = self.read_snapshot_rows(theirs_head.snapshot_id)
        diff3.assert_same_schema(spec, spec_b, "三方快照 Schema 校验")

        plan = diff3.build_plan(
            spec, base_rows, ours_rows, theirs_rows,
            base_id, ours_head.snapshot_id, theirs_head.snapshot_id,
        )
        classifications = {
            k: {"key": e.key, "classification": e.classification,
                "conflict": e.conflict, "reason": e.reason}
            for k, e in plan.entries.items()
        }
        log.plan_built(
            classifications,
            [{"key": c.key, "classification": c.classification, "reason": c.reason}
             for c in plan.conflicts.values()],
            auto_count=len(plan.entries) - len(plan.conflicts),
            conflict_count=len(plan.conflicts),
            include_detail=True,
        )

        run = MergeRun(
            run_id=run_id,
            table=table,
            status=MergeStatus.OPEN.value,
            base_snapshot_id=base_id,
            ours_snapshot_id=ours_head.snapshot_id,
            theirs_snapshot_id=theirs_head.snapshot_id,
            ours_branch=ours_branch,
            theirs_branch=theirs_branch,
            plan=plan,
            created_at=utc_now_iso(),
        )
        self.store.insert_merge_run(run)
        return run

    def _logger(self, run_id: str) -> RunLogger:
        return RunLogger(self.cfg.log_dir, run_id, echo=self.log_echo)

    def get_run(self, run_id: str) -> MergeRun:
        run = self.store.get_merge_run(run_id)
        if run is None:
            raise NotFoundError(f"合并运行不存在: {run_id}")
        return run

    # ============================================================== 合并：解决

    def resolve_conflict(
        self,
        run_id: str,
        key: list[Any] | dict[str, Any],
        kind: str,
        custom_row: dict[str, Any] | None = None,
        binding: tuple[str, str, str] | None = None,
    ) -> MergeRun:
        run = self.get_run(run_id)
        log = self._logger(run_id)
        if run.status == MergeStatus.COMMITTED.value:
            raise MergeAlreadyCommittedError(f"合并 {run_id} 已提交，不能再修改")

        # 解决方案必须绑定开启时的三方快照身份
        if binding is not None and tuple(binding) != run.binding():
            log.failed("resolve", "resolution_binding_mismatch",
                       f"解决方案绑定 {binding} 与合并三方快照 {run.binding()} 不一致")
            raise BindingMismatchError(
                "解决方案绑定的三方快照与当前合并不一致；请基于当前快照重新开启合并"
            )

        ks = self._key_str(run, key)
        if ks not in plan_conflicts(run):
            log.rejected(ks, "该键不是未决冲突", ResolutionRejectedError.code)
            raise ResolutionRejectedError(f"键 {ks} 不是该合并的未决冲突")

        entry = run.plan.entries[ks]
        try:
            final_row, deleted = diff3.apply_resolution(entry, run.plan.primary_key,
                                                        kind, custom_row)
        except ResolutionRejectedError:
            log.rejected(ks, f"解决方案 {kind} 不适用", ResolutionRejectedError.code)
            raise

        # VALUE 方案的字段类型/缺失列用 Schema 严格校验
        if final_row is not None and kind == ResolutionKind.VALUE.value:
            spec = self.get_spec(run.table)
            final_row = _validate_custom_row(spec, entry, final_row, kind)

        record = run.plan.conflicts[ks]
        record.resolution = {
            "kind": kind,
            "custom_row": custom_row,
            "final_row": final_row,
            "deleted": deleted,
            "bound_to": list(run.binding()),
            "resolved_at": utc_now_iso(),
        }
        entry.merged = final_row
        entry.deleted = deleted
        entry.conflict = False
        # 解决后整行来自显式决定：字段血缘统一标记 resolution 并写入最终值
        if final_row is not None:
            for fname, dec in entry.fields.items():
                dec.origin = FieldOrigin.RESOLUTION.value
                dec.value = final_row[fname]

        log.resolved(ks, entry.classification, kind, run.binding(),
                     {"deleted": deleted, "final_row": final_row})
        self.store.save_merge_run(run)
        return run

    def _key_str(self, run: MergeRun, key: list[Any] | dict[str, Any]) -> str:
        if isinstance(key, dict):
            parts = [key[pk] for pk in run.plan.primary_key]
        else:
            parts = list(key)
        return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))

    # ============================================================== 合并：提交

    def commit_merge(self, run_id: str, message: str | None = None,
                     target_branch: str | None = None) -> Snapshot:
        run = self.get_run(run_id)
        log = self._logger(run_id)
        if run.status == MergeStatus.COMMITTED.value:
            raise MergeAlreadyCommittedError(f"合并 {run_id} 已提交")

        unresolved = run.plan.unresolved
        if unresolved:
            detail = [{"key": c.key, "classification": c.classification}
                      for c in unresolved.values()]
            log.failed("commit", "unresolved_conflicts",
                       f"仍有 {len(unresolved)} 个未解决冲突: {detail}")
            raise UnresolvedConflictError(
                f"仍有 {len(unresolved)} 个未解决冲突，拒绝提交: {detail}"
            )

        final_rows = self._assemble_rows(run)
        spec = self.get_spec(run.table)
        # 组装结果同样过完整 Schema 校验，类型错误不会在提交时变成"成功"
        pa_table, count = arrow_format.rows_to_table(final_rows, spec)
        canonical, content_hash, _ = arrow_format.canonical_content(spec, final_rows)

        target = target_branch or run.theirs_branch
        old_main_head = self.get_branch_head(run.table, target).snapshot_id
        if old_main_head != run.theirs_snapshot_id:
            raise ConflictError(
                f"目标分支 {target} 在合并期间已前进 "
                f"({run.theirs_snapshot_id[:12]} -> {old_main_head[:12]})，"
                "请基于新头重新合并；禁止覆盖分支历史"
            )

        try:
            with self.store.transaction() as conn:
                snapshot_id = f"snap_{uuid.uuid4().hex[:16]}"
                path = self._parquet_path(snapshot_id)
                arrow_format.write_parquet(pa_table, path)
                snap = Snapshot(
                    snapshot_id=snapshot_id,
                    table=run.table,
                    schema_version=1,
                    content_hash=content_hash,
                    row_count=count,
                    parent_snapshot_id=run.ours_snapshot_id,
                    created_by_run_id=run.run_id,
                    created_at=utc_now_iso(),
                )
                conn.execute(
                    "INSERT INTO snapshots(snapshot_id, table_name, schema_version, "
                    "content_hash, row_count, parent_snapshot_id, created_by_run_id, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (snap.snapshot_id, snap.table, snap.schema_version, snap.content_hash,
                     snap.row_count, snap.parent_snapshot_id, snap.created_by_run_id,
                     snap.created_at),
                )
                # 提交后保留两条父引用：0=开发头，1=主头
                for pos, pid in enumerate([run.ours_snapshot_id, run.theirs_snapshot_id]):
                    conn.execute(
                        "INSERT INTO snapshot_parents(snapshot_id, parent_snapshot_id, position) "
                        "VALUES(?, ?, ?)",
                        (snapshot_id, pid, pos),
                    )
                if not self.store.advance_branch(
                    conn, target, run.table, old_main_head, snapshot_id
                ):
                    raise ConflictError("分支前进失败（并发修改），事务回滚")

                run.status = MergeStatus.COMMITTED.value
                run.committed_snapshot_id = snapshot_id
                run.commit_message = message
                run.committed_at = utc_now_iso()
                conn.execute(
                    "INSERT INTO merge_runs(run_id, table_name, status, ours_branch, "
                    "theirs_branch, base_snapshot_id, ours_snapshot_id, theirs_snapshot_id, "
                    "plan_json, committed_snapshot_id, commit_message, created_at, committed_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, "
                    "plan_json=excluded.plan_json, committed_snapshot_id=excluded.committed_snapshot_id, "
                    "commit_message=excluded.commit_message, committed_at=excluded.committed_at",
                    (
                        run.run_id, run.table, run.status, run.ours_branch, run.theirs_branch,
                        run.base_snapshot_id, run.ours_snapshot_id, run.theirs_snapshot_id,
                        json.dumps(run.plan.to_dict(), ensure_ascii=False),
                        snapshot_id, message, run.created_at, run.committed_at,
                    ),
                )
        except ConflictError:
            log.failed("commit", ConflictError.code, "分支前进失败，事务已回滚")
            raise
        except (sqlite3.Error, OSError) as e:
            log.failed("commit", "storage_error", str(e))
            raise

        log.committed(snapshot_id, count,
                      [run.ours_snapshot_id, run.theirs_snapshot_id],
                      target, old_main_head)
        return self.store.get_snapshot(snapshot_id)  # type: ignore[return-value]

    def _assemble_rows(self, run: MergeRun) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for entry in run.plan.entries.values():
            if entry.deleted:
                continue
            if entry.merged is None:
                raise UnresolvedConflictError(f"键 {entry.key} 没有合并结果")
            rows.append(entry.merged)
        rows.sort(key=lambda r: tuple(
            _sortable(r[pk]) for pk in run.plan.primary_key
        ))
        return rows

    def abandon_merge(self, run_id: str) -> MergeRun:
        run = self.get_run(run_id)
        if run.status == MergeStatus.COMMITTED.value:
            raise MergeAlreadyCommittedError(f"合并 {run_id} 已提交，不能放弃")
        run.status = MergeStatus.ABANDONED.value
        self.store.save_merge_run(run)
        return run

    # ============================================================== 血缘

    def lineage_graph(self, table: str) -> dict[str, Any]:
        self.get_spec(table)
        pm = self.store.parent_map(table)
        snaps = {s.snapshot_id: s for s in self.store.list_snapshots(table)}
        nodes = [
            {
                "snapshot_id": sid,
                "row_count": s.row_count,
                "content_hash": s.content_hash,
                "created_by_run_id": s.created_by_run_id,
                "created_at": s.created_at,
            }
            for sid, s in snaps.items()
        ]
        edges = [
            {"child": sid, "parent": pid}
            for sid, plist in pm.items() for pid in plist
        ]
        return {"table": table, "nodes": nodes, "edges": edges,
                "branches": [b.__dict__ for b in self.store.list_branches(table)]}


# ---------------------------------------------------------------- 辅助

def plan_conflicts(run: MergeRun) -> dict[str, ConflictRecord]:
    return run.plan.conflicts


def _field(f: dict[str, Any]) -> Any:
    from ..domain.models import FieldSpec
    allowed = {"name", "type", "nullable"}
    if not f.get("name") or not f.get("type"):
        raise ValidationError(f"字段定义必须含 name/type: {f}")
    return FieldSpec(**{k: v for k, v in f.items() if k in allowed})


def _validate_custom_row(spec: TableSpec, entry: Any, final_row: dict[str, Any],
                         kind: str) -> dict[str, Any]:
    from ..adapters.arrow_format import rows_to_table
    type_by_name = {fld.name: fld.type for fld in spec.fields}
    allowed = set(type_by_name)
    unknown = [c for c in final_row if c not in allowed]
    if unknown:
        raise ResolutionRejectedError(f"解决方案含未知字段: {unknown}")

    cls = entry.classification
    if cls == "field_value_conflict":
        # 非冲突字段已在内核中并入 entry.merged；用它补齐成完整行（含主键），
        # 再整体过 Schema 类型校验。
        full = dict(entry.merged)
        full.update(final_row)
        rows_to_table([full], spec)
        return full
    else:
        missing = [fld.name for fld in spec.fields if fld.name not in final_row]
        if missing:
            raise ResolutionRejectedError(f"解决方案缺少字段: {missing}")
        rows_to_table([final_row], spec)
        return final_row


def _sortable(v: Any) -> Any:
    return (type(v).__name__, v)
