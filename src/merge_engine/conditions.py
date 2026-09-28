"""受限条件表达式：MERGE 三类条件（update / insert / delete）的解析与求值。

不用 eval——语法是显式 JSON 树：

    {"all": [
        {"op": "eq",
         "left": {"side": "target", "column": "status"},
         "right": {"literal": "ACTIVE"}},
        {"op": "gte",
         "left": {"side": "source", "column": "score"},
         "right": {"side": "target", "column": "score"}}
    ]}

支持：
  组合节点  all(AND) / any(OR) / not
  二元谓词  eq ne gt gte lt lte
  一元谓词  is_null is_not_null
  操作数    {"side": "source"|"target", "column": ...} 或 {"literal": ...}

NULL 语义（标准 SQL 三值逻辑）：
  - 任一比较操作数为 NULL            -> NULL（既不是 TRUE 也不是 FALSE）
  - not NULL -> NULL；AND/OR 按三值逻辑短路
  - 决策时只有 TRUE 通过；NULL 按 FALSE 处理，但 reason 中显式标注 *_NULL，
    使“条件为假”和“条件因 NULL 不成立”可区分。

跨类型排序采用 SQLite 风格的全序（NULL < 数值 < 文本 < 字节串），
保证任何输入都有确定结果，不抛 Python 类型异常。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .errors import ConfigError, ConditionEvaluationFailure

BINARY_OPS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})
UNARY_OPS = frozenset({"is_null", "is_not_null"})


class Tri(str, Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    NULL = "NULL"


# ---- AST -------------------------------------------------------------------

@dataclass(frozen=True)
class Operand:
    kind: str            # "column" | "literal"
    side: str | None = None   # source | target（仅 column）
    column: str | None = None
    value: Any = None


@dataclass(frozen=True)
class Predicate:
    op: str
    left: Operand | None = None
    right: Operand | None = None


@dataclass(frozen=True)
class Condition:
    # 三选一（叶节点时 pred 非空）
    op: str                      # all | any | not | pred:<op>
    children: tuple["Condition", ...] = ()
    pred: Predicate | None = None
    path: str = ""               # 解析路径，用于 reason 定位


# ---- 解析（含契约校验） ------------------------------------------------------

@dataclass(frozen=True)
class ConditionScope:
    name: str                       # "update" | "insert" | "delete"
    allowed_sides: frozenset[str]   # 允许引用的行侧


def parse_condition(
    obj: Any,
    scope: ConditionScope,
    known_columns: frozenset[str],
    path: str = "$",
) -> Condition:
    if not isinstance(obj, dict):
        raise ConfigError(f"{scope.name} condition at {path} must be an object",
                          details={"path": path})

    if "all" in obj:
        items = _as_list(obj["all"], f"{scope.name}.all", path)
        children = tuple(
            parse_condition(x, scope, known_columns, f"{path}.all[{i}]")
            for i, x in enumerate(items)
        )
        return Condition("all", children, path=path)
    if "any" in obj:
        items = _as_list(obj["any"], f"{scope.name}.any", path)
        children = tuple(
            parse_condition(x, scope, known_columns, f"{path}.any[{i}]")
            for i, x in enumerate(items)
        )
        return Condition("any", children, path=path)
    if "not" in obj:
        child = parse_condition(obj["not"], scope, known_columns, f"{path}.not")
        return Condition("not", (child,), path=path)

    op = obj.get("op")
    if not isinstance(op, str):
        raise ConfigError(
            f"{scope.name} condition at {path} needs 'all'/'any'/'not' or a predicate 'op'",
            details={"path": path, "keys": sorted(obj.keys()) if isinstance(obj, dict) else []},
        )

    if op in UNARY_OPS:
        arg = _parse_operand(obj.get("arg"), scope, known_columns, f"{path}.arg")
        return Condition(f"pred:{op}", pred=Predicate(op, left=arg), path=path)
    if op in BINARY_OPS:
        left = _parse_operand(obj.get("left"), scope, known_columns, f"{path}.left")
        right = _parse_operand(obj.get("right"), scope, known_columns, f"{path}.right")
        return Condition(f"pred:{op}", pred=Predicate(op, left, right), path=path)

    raise ConfigError(f"unknown operator {op!r} in {scope.name} condition at {path}",
                      details={"path": path, "op": op})


def _as_list(value: Any, label: str, path: str) -> list:
    if not isinstance(value, list):
        raise ConfigError(f"{label} at {path} must be a list", details={"path": path})
    return value


def _parse_operand(
    obj: Any,
    scope: ConditionScope,
    known_columns: frozenset[str],
    path: str,
) -> Operand:
    if not isinstance(obj, dict):
        raise ConfigError(f"operand at {path} must be an object", details={"path": path})
    if "literal" in obj:
        value = obj["literal"]
        if not _is_scalar(value):
            raise ConfigError(f"literal at {path} must be a scalar",
                              details={"path": path, "type": type(value).__name__})
        return Operand("literal", value=value)
    if "column" in obj:
        column = obj["column"]
        side = obj.get("side")
        if not isinstance(column, str):
            raise ConfigError(f"column name at {path} must be a string",
                              details={"path": path})
        if side not in ("source", "target"):
            raise ConfigError(f"column operand at {path} needs side 'source' or 'target'",
                              details={"path": path, "side": side})
        if side not in scope.allowed_sides:
            raise ConfigError(
                f"{scope.name} condition cannot reference {side} columns at {path}",
                details={"path": path, "side": side, "allowed": sorted(scope.allowed_sides)},
            )
        if column not in known_columns:
            raise ConfigError(f"unknown column {column!r} at {path}",
                              details={"path": path, "column": column,
                                       "known_columns": sorted(known_columns)})
        return Operand("column", side=side, column=column)
    raise ConfigError(f"operand at {path} must be {{'column':..}} or {{'literal':..}}",
                      details={"path": path})


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str, bytes))


# ---- 三值逻辑求值 ------------------------------------------------------------

_TYPE_RANK = {type(None): 0, bool: 1, int: 1, float: 1, str: 2, bytes: 3}


def _rank(v: Any) -> int:
    t = type(v)
    if t in _TYPE_RANK:
        return _TYPE_RANK[t]
    # 非常规标量按类名稳定排序，避免异常
    return 4


def _compare(op: str, a: Any, b: Any) -> Tri:
    if a is None or b is None:
        return Tri.NULL
    try:
        ra, rb = _rank(a), _rank(b)
        if ra != rb:
            result = ra < rb
        else:
            result = a < b
        if op == "lt":
            ok = result
        elif op == "lte":
            ok = result or a == b
        elif op == "gt":
            ok = (not result) and (a != b)
        elif op == "gte":
            ok = (not result) or (a == b)
        elif op == "eq":
            ok = ra == rb and a == b
        else:  # ne
            ok = not (ra == rb and a == b)
        return Tri.TRUE if ok else Tri.FALSE
    except TypeError as exc:  # 理论上不会到达——全序兜底
        raise ConditionEvaluationFailure(
            f"incomparable values for {op}: {type(a).__name__} vs {type(b).__name__}",
            details={"op": op},
        ) from exc


def _resolve(operand: Operand, source: dict[str, Any] | None,
             target: dict[str, Any] | None) -> Any:
    if operand.kind == "literal":
        return operand.value
    row = source if operand.side == "source" else target
    if row is None:
        # 配置校验阶段已限制 insert 不可引用 target / delete 不可引用 source，
        # 到达这里说明内核装配有误，属于计算失败而非输入问题。
        raise ConditionEvaluationFailure(
            f"{operand.side} row unavailable for column {operand.column!r}",
            details={"column": operand.column, "side": operand.side},
        )
    return row.get(operand.column)


def _eval_predicate(pred: Predicate, source: dict[str, Any] | None,
                    target: dict[str, Any] | None) -> Tri:
    left = _resolve(pred.left, source, target)
    if pred.op in UNARY_OPS:
        is_null = left is None
        return Tri.TRUE if (is_null) == (pred.op == "is_null") else Tri.FALSE
    right = _resolve(pred.right, source, target)
    return _compare(pred.op, left, right)


def evaluate(cond: Condition, source: dict[str, Any] | None = None,
             target: dict[str, Any] | None = None) -> Tri:
    if cond.op == "all":
        saw_null = False
        for child in cond.children:
            tri = evaluate(child, source, target)
            if tri is Tri.FALSE:
                return Tri.FALSE
            if tri is Tri.NULL:
                saw_null = True
        return Tri.NULL if saw_null else Tri.TRUE
    if cond.op == "any":
        saw_null = False
        for child in cond.children:
            tri = evaluate(child, source, target)
            if tri is Tri.TRUE:
                return Tri.TRUE
            if tri is Tri.NULL:
                saw_null = True
        return Tri.NULL if saw_null else Tri.FALSE
    if cond.op == "not":
        tri = evaluate(cond.children[0], source, target)
        if tri is Tri.NULL:
            return Tri.NULL
        return Tri.FALSE if tri is Tri.TRUE else Tri.TRUE
    return _eval_predicate(cond.pred, source, target)
