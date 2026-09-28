"""操作前目标快照：指纹、复合键索引与目标重复键检测。

planner 只通过这里构建的 *不可变* 索引观察目标——提交阶段同批 INSERT 的结果
不会回流到索引中，从结构上保证“匹配使用操作前目标快照”。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .contracts import Key, NullEquality, TargetRow, jsonable
from .errors import KeyNullRejectedError, TargetDuplicateKeyError


def row_key(values: dict[str, Any], key_columns: tuple[str, ...]) -> Key:
    """提取复合键。缺列与显式 NULL 都视为 NULL。"""
    return tuple(values.get(col) for col in key_columns)


def key_jsonable(key: Key) -> list[Any]:
    return jsonable(list(key))


def keys_equal(a: Key, b: Key, strategy: NullEquality) -> bool:
    """复合键相等。

    SQL 策略：调用方需先保证两个键都不含 NULL（含 NULL 的源键在 planner 被拒绝）；
    DISTINCT 策略：NULL==NULL，等价于 SQL 的 IS NOT DISTINCT FROM。
    """
    for x, y in zip(a, b):
        if x is None or y is None:
            if strategy is NullEquality.DISTINCT and x is None and y is None:
                continue
            return False
        if x != y:
            return False
    return True


@dataclass(frozen=True)
class TargetSnapshot:
    table: str
    key_columns: tuple[str, ...]
    rows: tuple[TargetRow, ...]
    fingerprint: str
    # 非 NULL 键的精确索引（dict 相等 + 类型一致）
    index: dict[Key, TargetRow]
    # DISTINCT 策略下含 NULL 键的目标行，单独线性匹配
    nullable_rows: tuple[TargetRow, ...]


def snapshot_fingerprint(
    table: str,
    key_columns: tuple[str, ...],
    rows: list[tuple[int, dict[str, Any]]],
) -> str:
    """操作前快照指纹：表名 + 键列 + (rowid, 值) 的规范化序列化哈希。

    rowid 唯一，sorted 只在元组首位整数上比较，dict 永不参与排序比较。
    """
    body = jsonable([table, list(key_columns), sorted(rows)])
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_snapshot(
    table: str,
    key_columns: tuple[str, ...],
    raw_rows: list[tuple[int, dict[str, Any]]],
    strategy: NullEquality,
) -> TargetSnapshot:
    fingerprint = snapshot_fingerprint(table, key_columns, raw_rows)
    rows = tuple(TargetRow(rowid=rid, values=vals) for rid, vals in raw_rows)

    # 目标重复键检测：同一操作前快照内同键多行 = 状态冲突。
    # 注意 NULL：SQL 策略下 NULL 互不相等，不算重复（且永远匹配不上）；
    # DISTINCT 策略下 NULL==NULL，全 NULL 键也可能重复。
    seen: dict[Key, int] = {}
    dup_index: dict[Key, list[int]] = {}
    index: dict[Key, TargetRow] = {}
    nullable: list[TargetRow] = []

    for row in rows:
        key = row_key(row.values, key_columns)
        has_null = any(v is None for v in key)

        probe = key
        if has_null:
            if strategy is NullEquality.SQL:
                # SQL：NULL 键永不匹配，也不互相重复
                nullable.append(row)
                continue
            # DISTINCT：NULL 是普通值，继续走相等检查（线性，不进 dict 索引）
            matched = next((r for r in nullable if keys_equal(
                row_key(r.values, key_columns), probe, strategy)), None)
            if matched is not None:
                dup_index.setdefault(probe, [matched.rowid]).append(row.rowid)
            else:
                nullable.append(row)
            continue

        if probe in seen:
            dup_index.setdefault(probe, [seen[probe]]).append(row.rowid)
        else:
            seen[probe] = row.rowid
            index[probe] = row

    if dup_index:
        duplicates = [
            {
                "key": key_jsonable(k),
                "target_rowids": rids,
                "count": len(rids),
            }
            for k, rids in sorted(
                dup_index.items(), key=lambda item: json.dumps(key_jsonable(item[0]),
                                                                sort_keys=True)
            )
        ]
        raise TargetDuplicateKeyError(duplicates)

    return TargetSnapshot(
        table=table,
        key_columns=key_columns,
        rows=rows,
        fingerprint=fingerprint,
        index=index,
        nullable_rows=tuple(nullable),
    )


def lookup(snapshot: TargetSnapshot, key: Key) -> TargetRow | None:
    if any(v is None for v in key):
        # SQL 策略下源 NULL 键不会到达这里（planner 已拒绝）；
        # DISTINCT 策略下在线性区查找。
        for row in snapshot.nullable_rows:
            if keys_equal(row_key(row.values, snapshot.key_columns), key, NullEquality.DISTINCT):
                return row
        return None
    return snapshot.index.get(key)


def reject_sql_null_source_keys(
    source_keys: list[tuple[int, Key]],
) -> None:
    """SQL 策略：任何源行键列含 NULL 即拒绝（不依赖行序，一次性收集全部）。"""
    bad = [
        {"rownum": rn, "key": key_jsonable(key)}
        for rn, key in source_keys
        if any(v is None for v in key)
    ]
    if bad:
        raise KeyNullRejectedError(
            f"{len(bad)} source row(s) have NULL in key columns under SQL equality",
            details={"rows": bad},
        )
