"""执行内核（决策部分）：源批 + 操作前目标快照 -> MergePlan。

关键不变量（对应题目第二阶段）：

1. 源内同键多行：在接触目标之前一次性收集全部冲突并拒绝，结果与行序无关。
2. 匹配只查询 ``snapshot.lookup``——目标索引在构建快照时冻结，
   同批前一行的 INSERT 不会出现在索引中，因此不可能被后一行“匹配到”。
3. NULL 相等策略来自 spec；源 NULL 键在 SQL 策略下先于匹配被拒绝。
4. planner 不做任何写库动作，也不抛“提交期”错误；它只产出动作集合，
   写库是 store.apply_plan 在单个事务里的事（验证全部通过后才提交）。

动作产出顺序是确定性的：源行按 rownum 升序，未匹配目标删除按 rowid 升序。
"""
from __future__ import annotations

import json
from typing import Any

from .adapter import SourceBatch
from .conditions import Tri, evaluate
from .config import MergeSpec
from .contracts import (
    Action,
    ActionType,
    MergePlan,
)
from .errors import SourceDuplicateKeyError
from .snapshot import (
    build_snapshot,
    key_jsonable,
    lookup,
    reject_sql_null_source_keys,
    row_key,
)

# 判定理由（稳定字符串，测试与日志直接引用）
REASON_MATCHED_UPDATE = "MATCHED_UPDATE_COND_TRUE"
REASON_MATCHED_KEEP = "MATCHED_UPDATE_COND_FALSE"
REASON_MATCHED_NULL = "MATCHED_UPDATE_COND_NULL"
REASON_UNMATCHED_INSERT = "UNMATCHED_INSERT_COND_TRUE"
REASON_UNMATCHED_NO_INSERT = "UNMATCHED_INSERT_COND_FALSE"
REASON_UNMATCHED_INSERT_NULL = "UNMATCHED_INSERT_COND_NULL"
REASON_TARGET_DELETE = "DELETE_COND_TRUE"
REASON_TARGET_KEEP = "DELETE_COND_FALSE"
REASON_TARGET_DELETE_NULL = "DELETE_COND_NULL"


def plan_merge(
    batch: SourceBatch,
    spec: MergeSpec,
    target_rows: list[tuple[int, dict[str, Any]]],
) -> MergePlan:
    """纯决策函数。target_rows 为操作前快照 [(rowid, values), ...]。"""
    key_cols = spec.key_columns

    # ---- 阶段 1：源键提取（不依赖目标，也不依赖行序） ----------------------
    source_keys: list[tuple[int, tuple[Any, ...]]] = [
        (row.rownum, row_key(row.values, key_cols)) for row in batch.rows
    ]

    # ---- 阶段 2：源内重复键（一次性收集，按首次出现位置稳定排序） ----------
    first_seen: dict[tuple[Any, ...], int] = {}
    groups: dict[tuple[Any, ...], list[int]] = {}
    for rownum, key in source_keys:
        if key in groups:
            groups[key].append(rownum)
        elif key in first_seen:
            groups[key] = [first_seen[key], rownum]
        else:
            first_seen[key] = rownum
    if groups:
        duplicates = [
            {"key": key_jsonable(key), "rownums": rns}
            for key, rns in sorted(groups.items(), key=lambda kv: min(kv[1]))
        ]
        raise SourceDuplicateKeyError(duplicates)

    # ---- 阶段 3：SQL 策略下源 NULL 键拒绝（同样一次性收集） ---------------
    if spec.null_equality.value == "SQL":
        reject_sql_null_source_keys(source_keys)

    # ---- 阶段 4：冻结操作前目标快照（内部检测目标重复键 = 状态冲突） ------
    snapshot = build_snapshot(spec.target_table, key_cols, target_rows, spec.null_equality)

    batch_payload = tuple(c for c in batch.columns if c not in key_cols)
    # 目标表历史列（操作前快照中出现过的列）：after 行覆盖
    # “本批负载列 ∪ 目标既有负载列”；本批未携带的既有列显式置 NULL。
    target_columns: set[str] = set()
    for _, existing in target_rows:
        target_columns.update(existing.keys())
    write_payload = tuple(dict.fromkeys((
        *batch_payload,
        *sorted(target_columns - set(key_cols) - set(batch_payload)),
    )))
    payload_cols = write_payload
    actions: list[Action] = []
    counts = {t.value: 0 for t in ActionType}
    matched_rowids: set[int] = set()

    # ---- 阶段 5：逐源行决策（rownum 升序；只观察冻结快照） ----------------
    # 注意：matched_rowids 只用于后面的删除集合；匹配本身从不查 actions。
    for row in batch.rows:
        key = row_key(row.values, key_cols)
        target = lookup(snapshot, key)

        if target is not None:
            matched_rowids.add(target.rowid)
            tri, reason = _decide(spec.update_when, row.values, target.values,
                                  true_reason=REASON_MATCHED_UPDATE,
                                  false_reason=REASON_MATCHED_KEEP,
                                  null_reason=REASON_MATCHED_NULL)
            if tri is Tri.TRUE:
                after = _project_columns(row.values, key_cols, payload_cols)
                actions.append(Action(
                    type=ActionType.UPDATE_MATCHED, key=key,
                    source_rownum=row.rownum, target_rowid=target.rowid,
                    before=dict(target.values), after=after, reason=reason,
                ))
            else:
                actions.append(Action(
                    type=ActionType.NOOP_MATCHED, key=key,
                    source_rownum=row.rownum, target_rowid=target.rowid,
                    before=dict(target.values), reason=reason,
                ))
        else:
            tri, reason = _decide(spec.insert_when, row.values, None,
                                  true_reason=REASON_UNMATCHED_INSERT,
                                  false_reason=REASON_UNMATCHED_NO_INSERT,
                                  null_reason=REASON_UNMATCHED_INSERT_NULL)
            if tri is Tri.TRUE:
                after = _project_columns(row.values, key_cols, payload_cols)
                actions.append(Action(
                    type=ActionType.INSERT_UNMATCHED, key=key,
                    source_rownum=row.rownum, after=after, reason=reason,
                ))
            else:
                # 审计动作：记录“为什么没有插入”，提交阶段不写库
                actions.append(Action(
                    type=ActionType.NOOP_UNMATCHED, key=key,
                    source_rownum=row.rownum, reason=reason,
                ))

    # ---- 阶段 6：未匹配目标的条件删除（rowid 升序；新插入行不在快照中） ----
    if spec.delete_unmatched:
        for target in sorted(snapshot.rows, key=lambda r: r.rowid):
            if target.rowid in matched_rowids:
                continue
            tri, reason = _decide(spec.delete_when, None, target.values,
                                  true_reason=REASON_TARGET_DELETE,
                                  false_reason=REASON_TARGET_KEEP,
                                  null_reason=REASON_TARGET_DELETE_NULL)
            if tri is Tri.TRUE:
                actions.append(Action(
                    type=ActionType.DELETE_UNMATCHED,
                    key=row_key(target.values, key_cols),
                    target_rowid=target.rowid, before=dict(target.values),
                    reason=reason,
                ))
            # FALSE/NULL 的目标行静默保留（不产生动作、不写库）

    for action in actions:
        counts[action.type.value] += 1

    write_counts = {
        ActionType.UPDATE_MATCHED.value: counts[ActionType.UPDATE_MATCHED.value],
        ActionType.INSERT_UNMATCHED.value: counts[ActionType.INSERT_UNMATCHED.value],
        ActionType.DELETE_UNMATCHED.value: counts[ActionType.DELETE_UNMATCHED.value],
    }

    return MergePlan(
        target_table=spec.target_table,
        key_columns=key_cols,
        payload_columns=payload_cols,
        null_equality=spec.null_equality,
        actions=actions,
        snapshot_rowids=tuple(sorted(r.rowid for r in snapshot.rows)),
        snapshot_fingerprint=snapshot.fingerprint,
        write_counts=write_counts,
    )


def _decide(cond, source, target, *, true_reason, false_reason, null_reason):
    if cond is None:
        return Tri.TRUE, true_reason
    tri = evaluate(cond, source=source, target=target)
    if tri is Tri.TRUE:
        return Tri.TRUE, true_reason
    if tri is Tri.NULL:
        return Tri.NULL, null_reason
    return Tri.FALSE, false_reason


def _project_columns(
    values: dict[str, Any],
    key_cols: tuple[str, ...],
    payload_cols: tuple[str, ...],
) -> dict[str, Any]:
    """写库行的投影：键列 + 本批出现过的负载列，键列在前、列序稳定。

    UPDATE 语义：源批里出现的列以源值覆盖（稀疏行在适配层已按本批列集补 NULL，
    即该批次显式把该列置 NULL）；源批从未出现的目标列不在 after 中，
    由提交层保留目标原值。
    """
    return {col: values.get(col) for col in (*key_cols, *payload_cols)}


def plan_debug_summary(plan: MergePlan) -> dict[str, Any]:
    """供日志/测试核对的紧凑摘要。"""
    return {
        "fingerprint": plan.snapshot_fingerprint,
        "snapshot_n": len(plan.snapshot_rowids),
        "write_counts": plan.write_counts,
        "actions": [
            {"type": a.type.value, "key": json.dumps(key_jsonable(a.key)),
             "source_rownum": a.source_rownum, "target_rowid": a.target_rowid,
             "reason": a.reason}
            for a in plan.actions
        ],
    }
