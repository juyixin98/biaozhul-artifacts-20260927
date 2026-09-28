"""执行内核：不可变表快照的三方（base / dev / main）行级合并。

纯函数，不做任何数据库或文件 I/O。行身份 = 主键值；字段值逐列比较，
而不是只比文件名或整行哈希。

判定矩阵（K 为主键，B/D/M 分别为 base/dev/main 中该键的行）：

K 同时存在于三方：
  * D==B 且 M==B                -> UNCHANGED（取 B）
  * 仅一侧 != B                 -> FAST_FORWARD（取变化侧；含一侧删除）
  * 两侧都改、改动字段集合不相交 -> FIELD_MERGE（逐字段取变化侧）
  * 两侧都改、同一字段改成不同值 -> SAME_FIELD_CONFLICT
  * 同字段两侧改成相同值         -> FIELD_MERGE（收敛，非冲突）
  * 一侧删除、另一侧修改          -> DELETE_MODIFY_CONFLICT
K 不在 B：
  * 仅 D 或仅 M 新增             -> FAST_FORWARD
  * 两侧新增且内容相同            -> FIELD_MERGE（取任一侧，收敛）
  * 两侧新增且内容不同            -> ADD_ADD_CONFLICT
"""
from __future__ import annotations

from typing import Any

from .errors import InvalidResolutionError, SchemaMismatchError
from .models import (
    CONFLICT_ACTIONS,
    MergeReport,
    ResolutionAction,
    RowDecision,
    RowDecisionDetail,
    Snapshot,
    TableSchema,
    key_string,
    key_tuple,
)

# 冲突解决动作的行效果：USE_* / FIELD_PICK -> 保留所选行；KEEP_DELETED -> 不出现在结果中


def ensure_compatible_schemas(base: Snapshot, dev: Snapshot, main: Snapshot) -> TableSchema:
    """三方快照必须描述同一张表的同一 schema（列、类型、主键）。"""
    problems: list[str] = []
    if not (base.table == dev.table == main.table):
        problems.append(
            f"table names differ: {base.table!r} / {dev.table!r} / {main.table!r}"
        )
    if base.schema.column_names() != dev.schema.column_names() or \
            base.schema.column_names() != main.schema.column_names():
        problems.append("column lists differ across the three snapshots")
    if base.schema.type_map() != dev.schema.type_map() or \
            base.schema.type_map() != main.schema.type_map():
        problems.append("column types differ across the three snapshots")
    if base.schema.primary_key != dev.schema.primary_key or \
            base.schema.primary_key != main.schema.primary_key:
        problems.append("primary keys differ across the three snapshots")
    if problems:
        raise SchemaMismatchError(
            "three-way merge requires identical table schemas",
            details={
                "problems": problems,
                "base_snapshot_id": base.snapshot_id,
                "dev_snapshot_id": dev.snapshot_id,
                "main_snapshot_id": main.snapshot_id,
            },
        )
    return base.schema


def _changed_fields(base_row: dict, other_row: dict, schema: TableSchema) -> tuple[str, ...]:
    return tuple(
        c.name for c in schema.columns
        if base_row.get(c.name) != other_row.get(c.name)
    )


def _classify_present_in_base(
    key: tuple,
    b: dict | None,
    d: dict | None,
    m: dict | None,
    schema: TableSchema,
) -> tuple[RowDecision, dict | None, str, tuple[str, ...], tuple[str, ...]]:
    """返回 (判定, 自动合并行或 None, 依据, dev 改动字段, main 改动字段)。"""
    key_repr = key_string(key)

    if d is not None and m is not None:
        dev_changed = _changed_fields(b, d, schema)
        main_changed = _changed_fields(b, m, schema)
        if not dev_changed and not main_changed:
            return (RowDecision.UNCHANGED, b,
                    f"key {key_repr}: neither side changed relative to base", (), ())
        if dev_changed and not main_changed:
            return (RowDecision.FAST_FORWARD, d,
                    f"key {key_repr}: only dev changed fields {list(dev_changed)}",
                    dev_changed, ())
        if main_changed and not dev_changed:
            return (RowDecision.FAST_FORWARD, m,
                    f"key {key_repr}: only main changed fields {list(main_changed)}",
                    (), main_changed)

        # 两侧都修改：字段级比较
        overlap = set(dev_changed) & set(main_changed)
        clash = [f for f in sorted(overlap) if d.get(f) != m.get(f)]
        if clash:
            return (RowDecision.SAME_FIELD_CONFLICT, None,
                    f"key {key_repr}: both sides changed same field(s) {clash} "
                    f"to different values",
                    tuple(dev_changed), tuple(main_changed))

        # 不相交，或重叠字段两侧改成相同值（收敛）
        merged: dict[str, Any] = {}
        for col in schema.columns:
            if col.name in dev_changed:
                merged[col.name] = d[col.name]
            elif col.name in main_changed:
                merged[col.name] = m[col.name]
            else:
                merged[col.name] = b[col.name]
        converged = sorted(overlap)
        note = f"; overlapping fields {converged} converged to the same value" if converged else ""
        return (RowDecision.FIELD_MERGE, merged,
                f"key {key_repr}: disjoint field edits merged"
                f" (dev={list(dev_changed)}, main={list(main_changed)}){note}",
                tuple(dev_changed), tuple(main_changed))

    if d is None and m is None:
        return (RowDecision.FAST_FORWARD, None,
                f"key {key_repr}: both sides deleted it relative to base", (), ())

    # 一侧删除、另一侧存在
    surviving = d if d is not None else m
    side = "dev" if d is not None else "main"
    changed = _changed_fields(b, surviving, schema)
    if not changed:
        return (RowDecision.FAST_FORWARD, None,
                f"key {key_repr}: {side} kept it unchanged, other side deleted -> "
                f"fast-forward deletion",
                (), ())
    return (RowDecision.DELETE_MODIFY_CONFLICT, None,
            f"key {key_repr}: one side deleted while {side} modified fields "
            f"{list(changed)}",
            tuple(changed) if d is not None else (),
            tuple(changed) if m is not None else ())


def _classify_absent_in_base(
    key: tuple, d: dict | None, m: dict | None, schema: TableSchema
) -> tuple[RowDecision, dict | None, str]:
    key_repr = key_string(key)
    if d is not None and m is None:
        return (RowDecision.FAST_FORWARD, d,
                f"key {key_repr}: added only on dev")
    if m is not None and d is None:
        return (RowDecision.FAST_FORWARD, m,
                f"key {key_repr}: added only on main")
    # 两侧都新增
    if d == m:
        return (RowDecision.FIELD_MERGE, d,
                f"key {key_repr}: added identically on both sides (converged)")
    return (RowDecision.ADD_ADD_CONFLICT, None,
            f"key {key_repr}: added on both sides with different content")


def three_way_merge(
    schema: TableSchema,
    base_rows: list[dict[str, Any]],
    dev_rows: list[dict[str, Any]],
    main_rows: list[dict[str, Any]],
    *,
    base_snapshot_id: str = "base",
    dev_snapshot_id: str = "dev",
    main_snapshot_id: str = "main",
) -> MergeReport:
    """对行集做三方合并。调用方负责传入已按 schema 校验过的行。"""
    schema.validate()
    steps = [
        f"step 1/4: index rows by primary key {list(schema.primary_key)} "
        f"(base={len(base_rows)}, dev={len(dev_rows)}, main={len(main_rows)})",
    ]

    def index(rows: list[dict]) -> dict[tuple, dict]:
        out: dict[tuple, dict] = {}
        for row in rows:
            k = key_tuple(row, schema.primary_key)
            if k in out:
                raise ValueError(f"duplicate primary key {key_string(k)!r} in merge input")
            out[k] = row
        return out

    b_map = index(base_rows)
    d_map = index(dev_rows)
    m_map = index(main_rows)
    steps.append(
        f"step 2/4: partition {len(set(b_map) | set(d_map) | set(m_map))} keys by "
        "presence in base / dev / main"
    )

    all_keys = sorted(set(b_map) | set(d_map) | set(m_map), key=lambda k: key_string(k))
    decisions: list[RowDecisionDetail] = []
    merged_rows: list[dict] = []
    n_conflict = 0
    n_auto = 0

    for key in all_keys:
        b, d, m = b_map.get(key), d_map.get(key), m_map.get(key)
        if key in b_map:
            decision, row, basis, dc, mc = _classify_present_in_base(key, b, d, m, schema)
        else:
            decision, row, basis = _classify_absent_in_base(key, d, m, schema)
            dc = tuple(schema.column_names()) if d is not None else ()
            mc = tuple(schema.column_names()) if m is not None else ()

        detail = RowDecisionDetail(
            key=key, decision=decision, basis=basis,
            changed_fields_dev=dc, changed_fields_main=mc,
            dev_row=d, main_row=m,
        )
        decisions.append(detail)
        if decision.is_conflict:
            n_conflict += 1
        else:
            n_auto += 1
            if row is not None:
                merged_rows.append(row)

    steps.append(
        f"step 3/4: classified {len(all_keys)} keys -> {n_auto} auto-resolved, "
        f"{n_conflict} conflicts"
    )
    steps.append(
        "step 4/4: non-conflict partitions queued for auto-merge; conflict rows "
        "held back pending three-way-bound resolutions"
    )

    return MergeReport(
        base_snapshot_id=base_snapshot_id,
        dev_snapshot_id=dev_snapshot_id,
        main_snapshot_id=main_snapshot_id,
        merged_rows=merged_rows,
        decisions=decisions,
        steps=steps,
    )


def validate_resolution(
    decision: RowDecision,
    action: ResolutionAction | str,
    field_picks: dict[str, str] | None = None,
) -> None:
    """校验单个冲突的解决动作是否合法；非法时抛 InvalidResolutionError（不静默接受）。"""
    if isinstance(action, ResolutionAction):
        resolved_action = action
    else:
        try:
            resolved_action = ResolutionAction(action)
        except ValueError:
            raise InvalidResolutionError(
                f"unknown resolution action {action!r}",
                details={"action": action,
                         "valid": [a.value for a in ResolutionAction]},
            )
    action = resolved_action
    allowed = CONFLICT_ACTIONS.get(decision)
    if allowed is None:
        raise InvalidResolutionError(
            f"row decision {decision.value!r} is not a conflict and needs no resolution",
            details={"decision": decision.value},
        )
    if action not in allowed:
        raise InvalidResolutionError(
            f"action {action.value} is not allowed for {decision.value}; "
            f"allowed: {sorted(a.value for a in allowed)}",
            details={"decision": decision.value, "action": action.value,
                     "allowed": sorted(a.value for a in allowed)},
        )
    if action is ResolutionAction.FIELD_PICK:
        picks = field_picks or {}
        if not picks:
            raise InvalidResolutionError(
                "FIELD_PICK requires a non-empty field_picks mapping",
                details={"decision": decision.value},
            )
        bad = {f: s for f, s in picks.items() if s not in ("DEV", "MAIN")}
        if bad:
            raise InvalidResolutionError(
                "field_picks values must be 'DEV' or 'MAIN'",
                details={"bad": bad},
            )


def apply_resolutions(
    schema: TableSchema,
    report: MergeReport,
    resolutions: dict[tuple, dict],
) -> list[dict]:
    """把冲突解决应用到自动合并结果上，返回最终行集。

    resolutions: key -> {"action": ResolutionAction/str, "field_picks": {...可选}}
    决策必须与 report 绑定的三方快照一致（调用方按 plan 校验，这里校验行级合法性）。
    """
    conflict_map = {d.key: d for d in report.conflicts}
    missing = [key_string(k) for k in conflict_map if k not in resolutions]
    if missing:
        raise InvalidResolutionError(
            f"{len(missing)} conflict(s) left unresolved",
            details={"unresolved": missing},
        )
    unknown = [key_string(k) for k in resolutions if k not in conflict_map]
    if unknown:
        raise InvalidResolutionError(
            "resolutions reference keys that are not conflicts in this merge plan",
            details={"unknown": unknown},
        )

    result: dict[tuple, dict] = {
        key_tuple(row, schema.primary_key): row for row in report.merged_rows
    }
    for key, payload in resolutions.items():
        detail = conflict_map[key]
        action = payload["action"]
        action = ResolutionAction(action) if not isinstance(action, ResolutionAction) else action
        picks = payload.get("field_picks")
        validate_resolution(detail.decision, action, picks)

        if action is ResolutionAction.KEEP_DELETED:
            result.pop(key, None)
            continue

        if action is ResolutionAction.USE_DEV:
            chosen = detail.dev_row
        elif action is ResolutionAction.USE_MAIN:
            chosen = detail.main_row
        else:  # FIELD_PICK —— 以 base 为底，逐字段取所选一侧
            assert detail.dev_row is not None and detail.main_row is not None
            merged: dict[str, Any] = {}
            for col in schema.columns:
                side = (picks or {}).get(col.name)
                merged[col.name] = (detail.dev_row if side == "DEV" else detail.main_row)[col.name]
            chosen = merged
        if chosen is not None:
            result[key] = chosen

    return [result[k] for k in sorted(result, key=lambda k: key_string(k))]
