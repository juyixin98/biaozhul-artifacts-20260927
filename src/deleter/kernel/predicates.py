"""NULL 感知的键比较规则（SQL 三值逻辑的等值侧）。

规则（与测试中的独立参考实现逐条对应）：

1. 谓词值含 NULL：该谓词**永不命中任何行**。
   ``WHERE k = NULL`` 在 SQL 中求值为 UNKNOWN，不会过滤任何行。
2. 行键含 NULL：该行**永不被任何等值谓词命中**（即使谓词同位置也写 NULL，
   ``NULL = NULL`` 仍是 UNKNOWN，不是 TRUE）。
3. 复合键：所有组件都非 NULL 且逐组件相等，才算命中。
4. 类型不一致但数值上可比较时不做隐式跨类型转换（int/float 除外，
   二者同为数值时按 Python 语义比较；bool 不视作数值）。
"""
from __future__ import annotations

from typing import Any, Sequence

from .models import Row


def predicate_has_null(predicate_values: Sequence[Any]) -> bool:
    return any(v is None for v in predicate_values)


def row_key_has_null(row: Row, columns: Sequence[str]) -> bool:
    return any(row.values.get(c) is None for c in columns)


def values_equal(a: Any, b: Any) -> bool:
    """非 NULL 前提下方可调用；遵循"不做隐式跨类型转换"的约定。"""
    if a is None or b is None:
        raise ValueError("values_equal 只能比较非 NULL 值")
    # bool 是 int 子类，显式排除跨类比较：True != 1
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if type(a) is not type(b):
        return False
    return a == b


def row_matches(row: Row, columns: Sequence[str], values: Sequence[Any]) -> bool:
    """完整的等值命中判断（含 NULL 规则）。仅在两边均无 NULL 时才可能为 True。"""
    if predicate_has_null(values):
        return False
    if row_key_has_null(row, columns):
        return False
    for col, val in zip(columns, values):
        if not values_equal(row.values.get(col), val):
            return False
    return True
