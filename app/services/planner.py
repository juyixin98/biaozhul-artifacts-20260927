"""提交规划器：把多态操作列表解析、规范化并做全部语义校验。

POST /validate 只运行 plan()（不写任何文件/元数据）；
POST /commit 先 plan()，通过后再由 committer 落盘与提交元数据，
两条路径因此返回完全一致的错误类别与错误码。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.adapters import pyarrow_ops
from app.config import DEFAULT_LIMITS
from app.contracts.types import normalize_value
from app.errors import StateConflict, ValidationError

_APPEND = {"append", "rewrite", "position_delete", "equality_delete"}


@dataclass
class PlannedAppend:
    kind: str  # append / rewrite
    ref: str
    rows: list[dict[str, Any]]       # 已规范化的物理值
    drops: list[str] = field(default_factory=list)
    file_id: str = ""                # committer 分配后回填


@dataclass
class PlannedPositionDelete:
    ref: str | None
    target_file_id: str
    positions: list[int]
    delete_file_id: str = ""


@dataclass
class PlannedEqualityDelete:
    ref: str | None
    predicates: list[dict[str, Any]]  # 已规范化的键值（含显式 NULL）
    delete_file_id: str = ""


@dataclass
class Plan:
    table_id: str
    parent_snapshot_id: str | None
    schema_columns: list[dict[str, Any]]
    primary_key: list[str]
    limits: dict[str, int]
    appends: list[PlannedAppend]
    position_deletes: list[PlannedPositionDelete]
    equality_deletes: list[PlannedEqualityDelete]
    refs: dict[str, str]              # 客户端 ref -> 服务端新文件 id
    phases: list[dict[str, Any]] = field(default_factory=list)

    def log(self, phase: str, detail: dict[str, Any]) -> None:
        self.phases.append({"phase": phase, "detail": detail})


def plan_commit(
    store,
    *,
    table_id: str,
    parent_snapshot_id: str | None,
    operations: list[dict[str, Any]],
) -> Plan:
    table = store.get_table(table_id)
    if table is None:
        from app.errors import NotFound

        raise NotFound("TABLE_NOT_FOUND", f"table '{table_id}' not found")
    schema_columns = json.loads(table["schema_json"])
    primary_key = json.loads(table["primary_key"])
    limits = {**DEFAULT_LIMITS, **json.loads(table["config_json"])}
    type_by_name = {c["name"]: c["type"] for c in schema_columns}
    known = set(type_by_name)

    plan = Plan(
        table_id=table_id,
        parent_snapshot_id=parent_snapshot_id,
        schema_columns=schema_columns,
        primary_key=primary_key,
        limits=limits,
        appends=[],
        position_deletes=[],
        equality_deletes=[],
        refs={},
    )

    if not isinstance(operations, list) or not operations:
        raise ValidationError("EMPTY_COMMIT", "commit requires at least one operation")
    if len(operations) > limits["max_files_per_snapshot"] + limits["max_delete_files_per_snapshot"]:
        from app.errors import ResourceExhausted

        raise ResourceExhausted("TOO_MANY_OPERATIONS", "operation count exceeds configured limit")

    refs_seen: set[str] = set()

    for index, op in enumerate(operations):
        _require_mapping(op, f"operations[{index}]")
        kind = op.get("op")
        if kind not in _APPEND:
            raise ValidationError(
                "UNKNOWN_OP",
                f"operations[{index}]: op must be one of {sorted(_APPEND)}",
                {"index": index, "op": kind},
            )
        ref = op.get("ref")
        if ref is not None:
            if not isinstance(ref, str) or not ref:
                raise ValidationError("INVALID_REF", f"operations[{index}]: ref must be a non-empty string")
            if ref in refs_seen:
                raise ValidationError("DUPLICATE_REF", f"duplicate file ref {ref!r}", {"ref": ref})
            refs_seen.add(ref)

        if kind in ("append", "rewrite"):
            plan.appends.append(_plan_append(plan, index, op, kind, type_by_name, known, refs_seen))
        elif kind == "position_delete":
            plan.position_deletes.append(_plan_position(plan, index, op))
        else:
            plan.equality_deletes.append(_plan_equality(plan, index, op, primary_key, type_by_name))

    plan.log("operations_parsed", {
        "appends": len(plan.appends),
        "position_deletes": len(plan.position_deletes),
        "equality_deletes": len(plan.equality_deletes),
    })

    _validate_state(store, plan)
    return plan


def _require_mapping(op: Any, where: str) -> None:
    if not isinstance(op, dict):
        raise ValidationError("INVALID_OPERATION", f"{where} must be an object")


def _plan_append(
    plan: Plan,
    index: int,
    op: dict[str, Any],
    kind: str,
    type_by_name: dict[str, str],
    known: set[str],
    refs_seen: set[str],
) -> PlannedAppend:
    ref = op.get("ref")
    if not isinstance(ref, str) or not ref:
        raise ValidationError("INVALID_REF", f"operations[{index}]: append requires a non-empty 'ref'")
    rows = op.get("rows")
    if not isinstance(rows, list):
        raise ValidationError("INVALID_ROWS", f"operations[{index}]: 'rows' must be a list")
    if len(rows) > plan.limits["max_rows_per_data_file"]:
        from app.errors import ResourceExhausted

        raise ResourceExhausted(
            "TOO_MANY_ROWS",
            f"file {ref!r} exceeds max_rows_per_data_file={plan.limits['max_rows_per_data_file']}",
            {"ref": ref, "rows": len(rows)},
        )
    norm_rows = []
    for r_i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValidationError("INVALID_ROW", f"{ref}.rows[{r_i}] must be an object")
        unknown = sorted(set(row) - known)
        if unknown:
            raise ValidationError(
                "UNKNOWN_COLUMN", f"{ref}.rows[{r_i}] has unknown columns", {"columns": unknown}
            )
        norm_rows.append({name: normalize_value(row.get(name), t, column=name)
                          for name, t in type_by_name.items()})

    drops: list[str] = []
    if kind == "rewrite":
        drops = op.get("drops")
        if not isinstance(drops, list) or not drops:
            raise ValidationError(
                "REWRITE_REQUIRES_DROPS", f"operations[{index}]: rewrite requires non-empty 'drops'"
            )
        if any(not isinstance(d, str) or not d for d in drops):
            raise ValidationError("INVALID_DROP", f"operations[{index}]: drops must be non-empty file ids")
        if len(set(drops)) != len(drops):
            raise ValidationError("DUPLICATE_DROP", f"operations[{index}]: duplicate entries in drops")
    planned = PlannedAppend(kind=kind, ref=ref, rows=norm_rows, drops=drops)
    plan.log("append_planned", {"ref": ref, "kind": kind, "rows": len(norm_rows), "drops": drops})
    return planned


def _plan_position(plan: Plan, index: int, op: dict[str, Any]) -> PlannedPositionDelete:
    target = op.get("target_file")
    if not isinstance(target, str) or not target:
        raise ValidationError(
            "INVALID_TARGET", f"operations[{index}]: position_delete requires 'target_file' id"
        )
    positions = op.get("positions")
    if not isinstance(positions, list) or not positions:
        raise ValidationError(
            "INVALID_POSITIONS", f"operations[{index}]: 'positions' must be a non-empty list"
        )
    norm: list[int] = []
    for p in positions:
        if isinstance(p, bool) or not isinstance(p, int) or p < 0:
            raise ValidationError(
                "INVALID_POSITION", f"operations[{index}]: positions must be non-negative integers"
            )
        norm.append(p)
    if len(set(norm)) != len(norm):
        raise ValidationError(
            "DUPLICATE_POSITION", f"operations[{index}]: duplicate positions in one operation"
        )
    if len(norm) > plan.limits["max_rows_per_delete_file"]:
        from app.errors import ResourceExhausted

        raise ResourceExhausted(
            "TOO_MANY_DELETE_ROWS",
            f"position delete exceeds max_rows_per_delete_file={plan.limits['max_rows_per_delete_file']}",
        )
    planned = PlannedPositionDelete(ref=op.get("ref"), target_file_id=target, positions=sorted(norm))
    plan.log("position_delete_planned", {"target_file": target, "positions": len(norm)})
    return planned


def _plan_equality(
    plan: Plan, index: int, op: dict[str, Any], primary_key: list[str], type_by_name: dict[str, str]
) -> PlannedEqualityDelete:
    predicates = op.get("predicates")
    if not isinstance(predicates, list) or not predicates:
        raise ValidationError(
            "INVALID_PREDICATES", f"operations[{index}]: equality_delete requires non-empty 'predicates'"
        )
    norm_preds: list[dict[str, Any]] = []
    for pr_i, pred in enumerate(predicates):
        if not isinstance(pred, dict) or not isinstance(pred.get("key"), dict):
            raise ValidationError(
                "INVALID_PREDICATE",
                f"operations[{index}].predicates[{pr_i}] must be {{'key': {{col: value}}}}",
            )
        key = pred["key"]
        missing = [k for k in primary_key if k not in key]
        if missing:
            raise ValidationError(
                "MISSING_KEY_VALUE",
                f"operations[{index}].predicates[{pr_i}] missing key columns (explicit null required)",
                {"columns": missing},
            )
        unknown = sorted(set(key) - set(primary_key))
        if unknown:
            raise ValidationError(
                "UNKNOWN_KEY_COLUMN", "predicate contains non-key columns", {"columns": unknown}
            )
        norm_preds.append(
            {"key": {k: normalize_value(key[k], type_by_name[k], column=k) for k in primary_key}}
        )
    if len(norm_preds) > plan.limits["max_rows_per_delete_file"]:
        from app.errors import ResourceExhausted

        raise ResourceExhausted(
            "TOO_MANY_DELETE_ROWS",
            f"equality delete exceeds max_rows_per_delete_file={plan.limits['max_rows_per_delete_file']}",
        )
    planned = PlannedEqualityDelete(ref=op.get("ref"), predicates=norm_preds)
    plan.log("equality_delete_planned", {"predicates": len(norm_preds), "key_columns": primary_key})
    return planned


# ---------------------------------------------------------------------------
# 状态校验：父快照、文件存活、行号范围、序列号窗口
# ---------------------------------------------------------------------------
def _validate_state(store, plan: Plan) -> None:
    with store.read_conn() as conn:
        current = store.current_snapshot(conn, plan.table_id)
        # 根提交：parent 必须为 None 且表内尚无快照；后续提交必须显式携带当前快照 id
        if current is None and plan.parent_snapshot_id is not None:
            raise StateConflict(
                "PARENT_MISMATCH", "table has no snapshot yet; parent_snapshot_id must be null"
            )
        if current is not None:
            if plan.parent_snapshot_id is None:
                raise StateConflict(
                    "PARENT_REQUIRED",
                    "parent_snapshot_id is required for non-initial commits",
                    {"expected": current["snapshot_id"]},
                )
            if plan.parent_snapshot_id != current["snapshot_id"]:
                raise StateConflict(
                    "PARENT_MISMATCH",
                    "parent_snapshot_id is not the current snapshot (optimistic concurrency failure)",
                    {"given": plan.parent_snapshot_id, "expected": current["snapshot_id"]},
                )

        next_seq = 1 if current is None else current["seq"] + 1
        drop_targets = {d for a in plan.appends for d in a.drops}
        live_at_parent = {r["file_id"] for r in
                          store.live_files_at(conn, plan.table_id, next_seq - 1)}

        # 解析重写 drops
        for fid in sorted(drop_targets):
            row = store.get_data_file(conn, fid)
            if row is None or row["table_id"] != plan.table_id:
                # 语法上像 id 但系统中不存在 -> 状态冲突（不是字段形状问题）
                raise StateConflict("FILE_NOT_FOUND", f"drop target file '{fid}' does not exist")
            if fid not in live_at_parent:
                raise StateConflict(
                    "FILE_NOT_LIVE", f"file '{fid}' is not live at parent snapshot; cannot rewrite/drop it"
                )

        # 同批“先重写再对新文件位置删除”必须拒绝：位置删除不能指向同提交新增文件，
        # 因为 Iceberg v2 位置删除要求目标文件 seq < 删除文件 seq。
        new_refs = {a.ref for a in plan.appends}
        for pd in plan.position_deletes:
            if pd.target_file_id in new_refs:
                raise ValidationError(
                    "POSITION_TARGET_SAME_COMMIT",
                    "position delete cannot target a file added in the same commit (sequence window)",
                    {"target_file_ref": pd.target_file_id},
                )
            row = store.get_data_file(conn, pd.target_file_id)
            if row is None or row["table_id"] != plan.table_id:
                raise StateConflict(
                    "POSITION_TARGET_UNKNOWN",
                    f"position delete target '{pd.target_file_id}' does not exist",
                )
            if pd.target_file_id not in live_at_parent:
                raise StateConflict(
                    "STALE_POSITION_TARGET",
                    "position delete targets a file that was already rewritten/dropped at parent; "
                    "line numbers of the old content cannot be reused on the new file",
                    {"target_file_id": pd.target_file_id},
                )
            n = row["row_count"]
            bad = [p for p in pd.positions if p >= n]
            if bad:
                raise ValidationError(
                    "POSITION_OUT_OF_RANGE",
                    f"positions exceed target file row_count={n}",
                    {"target_file_id": pd.target_file_id, "positions": bad, "row_count": n},
                )

        # 数量上限（文件/删除文件）
        new_file_count = len(plan.appends)
        new_del_count = len(plan.position_deletes) + len(plan.equality_deletes)
        if new_file_count > plan.limits["max_files_per_snapshot"]:
            from app.errors import ResourceExhausted

            raise ResourceExhausted("TOO_MANY_FILES", "too many new data files in one snapshot")
        if new_del_count > plan.limits["max_delete_files_per_snapshot"]:
            from app.errors import ResourceExhausted

            raise ResourceExhausted("TOO_MANY_DELETE_FILES", "too many delete files in one snapshot")

        plan.log("state_validated", {
            "parent": None if current is None else current["snapshot_id"],
            "next_seq": next_seq,
            "drop_targets": sorted(drop_targets),
            "new_data_files": new_file_count,
            "new_delete_files": new_del_count,
        })
