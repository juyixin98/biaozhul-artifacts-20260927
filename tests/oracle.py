"""独立参考实现（oracle）——刻意不导入 merge_engine 的任何决策代码。

用途：随机化交叉核对。被测内核 (planner) 的动作集合必须与这个独立实现一致。
该 oracle 用最直白的命令式集合/字典操作重写语义，且：
  - 先整体扫描源构造冲突映射（不边读边判）；
  - 匹配用一次性冻结的 dict；
  - 动作顺序规则与内核相同（源 rownum 升序、删除 rowid 升序）。

只覆盖“成功路径”的动作集合与理由；错误路径由各用例手写断言。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# NULL 用 None；与内核共享“值表示”但不共享算法代码
TRI_TRUE, TRI_FALSE, TRI_NULL = "TRUE", "FALSE", "NULL"


@dataclass(frozen=True)
class OAction:
    type: str
    key: tuple[Any, ...]
    source_rownum: int | None
    target_rowid: int | None
    reason: str


def _key(row, key_cols):
    return tuple(row.get(c) for c in key_cols)


def _keys_eq(a, b, distinct_nulls):
    for x, y in zip(a, b):
        if x is None or y is None:
            if distinct_nulls and x is None and y is None:
                continue
            return False
        if x != y:
            return False
    return True


_RANK = {type(None): 0, bool: 1, int: 1, float: 1, str: 2, bytes: 3}


def _rank(v):
    return _RANK.get(type(v), 4)


def _cmp(op, a, b):
    if a is None or b is None:
        return TRI_NULL
    ra, rb = _rank(a), _rank(b)
    if ra != rb:
        lt = ra < rb
        eq = False
    else:
        lt = a < b
        eq = a == b
    table = {
        "lt": lt, "lte": lt or eq,
        "gt": (not lt) and (not eq), "gte": (not lt) or eq,
        "eq": eq, "ne": not eq,
    }
    return TRI_TRUE if table[op] else TRI_FALSE


def _resolve(operand, source, target):
    if "literal" in operand:
        return operand["literal"]
    side = operand["side"]
    row = source if side == "source" else target
    return row.get(operand["column"])


def _eval(node, source, target):
    if "all" in node:
        saw_null = False
        for child in node["all"]:
            t = _eval(child, source, target)
            if t == TRI_FALSE:
                return TRI_FALSE
            if t == TRI_NULL:
                saw_null = True
        return TRI_NULL if saw_null else TRI_TRUE
    if "any" in node:
        saw_null = False
        for child in node["any"]:
            t = _eval(child, source, target)
            if t == TRI_TRUE:
                return TRI_TRUE
            if t == TRI_NULL:
                saw_null = True
        return TRI_NULL if saw_null else TRI_FALSE
    if "not" in node:
        t = _eval(node["not"], source, target)
        return TRI_NULL if t == TRI_NULL else (TRI_FALSE if t == TRI_TRUE else TRI_TRUE)
    op = node["op"]
    if op in ("is_null", "is_not_null"):
        v = _resolve(node["arg"], source, target)
        ok = (v is None) == (op == "is_null")
        return TRI_TRUE if ok else TRI_FALSE
    a = _resolve(node["left"], source, target)
    b = _resolve(node["right"], source, target)
    return _cmp(op, a, b)


def _truth(cond, source, target):
    if cond is None:
        return TRI_TRUE
    return _eval(cond, source, target)


def oracle_plan(source_rows, target_rows, config):
    """返回 (error_code, actions)。成功时 error_code 为 None。

    source_rows: [{...}] ；target_rows: [(rowid, {...})]
    """
    kc = tuple(config["key_columns"])
    distinct = config.get("null_equality", "SQL") == "DISTINCT"
    update_when = config.get("update_when")
    insert_when = config.get("insert_when")
    do_delete = config.get("delete_unmatched", False)
    delete_when = config.get("delete_when")

    # 1) 源重复键（整体扫描；SQL/DISTINCT 对源重复判定相同：完全相等的键元组）
    groups = {}
    for i, row in enumerate(source_rows, start=1):
        k = _key(row, kc)
        groups.setdefault(k, []).append(i)
    dups = [k for k, rns in groups.items() if len(rns) > 1]
    if dups:
        return "SOURCE_DUPLICATE_KEY", []

    # 2) SQL 策略源 NULL 键
    if not distinct:
        if [i for i, r in enumerate(source_rows, 1)
                if any(v is None for v in _key(r, kc))]:
            return "KEY_NULL_REJECTED", []

    # 3) 冻结目标索引 + 目标重复检测
    frozen = {}
    dup_target = {}
    nullable = []
    for rid, row in target_rows:
        k = _key(row, kc)
        if any(v is None for v in k):
            if not distinct:
                nullable.append((rid, row, k))
                continue
            hit = next(((r, kk) for r, rr, kk in nullable if _keys_eq(kk, k, True)), None)
            if hit:
                dup_target.setdefault(k, [hit[0]]).append(rid)
            else:
                nullable.append((rid, row, k))
            continue
        if k in frozen:
            dup_target.setdefault(k, [frozen[k][0]]).append(rid)
        else:
            frozen[k] = (rid, row)
    if dup_target:
        return "TARGET_DUPLICATE_KEY", []

    def match(k):
        if any(v is None for v in k):
            for rid, row, kk in nullable:
                if _keys_eq(kk, k, True):
                    return rid, row
            return None
        return frozen.get(k)

    actions: list[OAction] = []
    matched = set()
    for i, srow in enumerate(source_rows, start=1):
        k = _key(srow, kc)
        hit = match(k)
        if hit is not None:
            rid, trow = hit
            matched.add(rid)
            t = _truth(update_when, srow, trow)
            if t == TRI_TRUE:
                actions.append(OAction("UPDATE_MATCHED", k, i, rid,
                                       "MATCHED_UPDATE_COND_TRUE"))
            elif t == TRI_NULL:
                actions.append(OAction("NOOP_MATCHED", k, i, rid,
                                       "MATCHED_UPDATE_COND_NULL"))
            else:
                actions.append(OAction("NOOP_MATCHED", k, i, rid,
                                       "MATCHED_UPDATE_COND_FALSE"))
        else:
            t = _truth(insert_when, srow, None)
            if t == TRI_TRUE:
                actions.append(OAction("INSERT_UNMATCHED", k, i, None,
                                       "UNMATCHED_INSERT_COND_TRUE"))
            elif t == TRI_NULL:
                actions.append(OAction("NOOP_UNMATCHED", k, i, None,
                                       "UNMATCHED_INSERT_COND_NULL"))
            else:
                actions.append(OAction("NOOP_UNMATCHED", k, i, None,
                                       "UNMATCHED_INSERT_COND_FALSE"))

    if do_delete:
        for rid, trow in sorted(target_rows):
            if rid in matched:
                continue
            t = _truth(delete_when, None, trow)
            if t == TRI_TRUE:
                actions.append(OAction("DELETE_UNMATCHED", _key(trow, kc), None,
                                       rid, "DELETE_COND_TRUE"))
    return None, actions
