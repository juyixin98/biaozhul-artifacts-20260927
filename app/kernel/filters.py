"""过滤 DSL 求值器。

语法：
- 比较：{"column": "c", "op": "=", "value": 5}
- 空值：{"column": "c", "op": "is_null"} / "not_null"
- 逻辑：{"and": [<pred>, ...]} / {"or": [...]} / {"not": <pred>}

求值采用 SQL 三值逻辑：NULL 比较结果为 NULL（按不满足处理，仅 is_null 可命中）；
类型不匹配（如对数值列传字符串）为输入错误而非“不匹配”。
列裁剪与过滤均不得改变删除语义：删除在内核中先于过滤应用（见 scanner）。
"""
from __future__ import annotations

from typing import Any

from app.errors import ValidationError

_SCALAR_OPS = {"=", "!=", "<>", "<", "<=", ">", ">=", "is_null", "not_null"}


def validate_filter_dsl(node: Any, known_columns: set[str]) -> None:
    if not isinstance(node, dict):
        raise ValidationError("INVALID_FILTER", "filter must be an object", {"node": node})
    keys = set(node)
    if "and" in keys or "or" in keys:
        if len(keys) != 1:
            raise ValidationError("INVALID_FILTER", "logical node must be {'and'|'or': [...]}")
        comb = "and" if "and" in keys else "or"
        children = node[comb]
        if not isinstance(children, list) or not children:
            raise ValidationError("INVALID_FILTER", f"'{comb}' requires a non-empty list")
        for child in children:
            validate_filter_dsl(child, known_columns)
        return
    if "not" in keys:
        if keys != {"not"}:
            raise ValidationError("INVALID_FILTER", "'not' node must be {'not': <predicate>}")
        validate_filter_dsl(node["not"], known_columns)
        return
    # 叶子谓词
    if "column" not in node or "op" not in node:
        raise ValidationError(
            "INVALID_FILTER", "predicate requires 'column' and 'op'", {"node": node}
        )
    col, op = node["column"], node["op"]
    if col not in known_columns:
        raise ValidationError("UNKNOWN_FILTER_COLUMN", f"unknown filter column '{col}'", {"column": col})
    if op not in _SCALAR_OPS:
        raise ValidationError("UNKNOWN_FILTER_OP", f"unsupported op {op!r}", {"op": op})
    if op not in ("is_null", "not_null") and "value" not in node:
        raise ValidationError("INVALID_FILTER", f"op {op!r} requires 'value'", {"node": node})


def evaluate(node: dict[str, Any], row: dict[str, Any]) -> bool:
    if "and" in node:
        return all(evaluate(c, row) for c in node["and"])
    if "or" in node:
        return any(evaluate(c, row) for c in node["or"])
    if "not" in node:
        return not evaluate(node["not"], row)
    col, op = node["column"], node["op"]
    val = row.get(col)
    if op == "is_null":
        return val is None
    if op == "not_null":
        return val is not None
    other = node["value"]
    if val is None or other is None:
        return False  # SQL NULL 语义：NULL 比较不满足
    if not isinstance(other, type(val)) and not _numeric_pair(val, other):
        raise ValidationError(
            "FILTER_TYPE_MISMATCH",
            f"filter value type {type(other).__name__} != column type {type(val).__name__}",
            {"column": col},
        )
    if op == "=":
        return val == other
    if op in ("!=", "<>"):
        return val != other
    if op == "<":
        return val < other
    if op == "<=":
        return val <= other
    if op == ">":
        return val > other
    if op == ">=":
        return val >= other
    raise ValidationError("UNKNOWN_FILTER_OP", f"unsupported op {op!r}")


def _numeric_pair(a: Any, b: Any) -> bool:
    # int/float 跨数值类型可比较（bool 已在规范化阶段排除）
    return (
        isinstance(a, (int, float))
        and isinstance(b, (int, float))
        and not isinstance(a, bool)
        and not isinstance(b, bool)
    )
