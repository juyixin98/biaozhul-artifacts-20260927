"""条件表达式测试：三值逻辑、跨类型全序、配置契约（侧/列引用限制）。"""
from __future__ import annotations

import pytest

from merge_engine.conditions import (
    Tri,
    evaluate,
    parse_condition,
)
from merge_engine.config import (
    DELETE_SCOPE,
    INSERT_SCOPE,
    UPDATE_SCOPE,
)
from merge_engine.errors import ConfigError

COLS = frozenset({"s", "t", "n"})


def P(obj, scope=UPDATE_SCOPE, cols=COLS):
    return parse_condition(obj, scope, cols)


def test_three_valued_logic_and_or_not():
    src = {"n": 10}
    # n >= 5 AND s = 'x'：NULL AND TRUE -> NULL
    cond = P({"all": [
        {"op": "gte", "left": {"side": "source", "column": "n"}, "right": {"literal": 5}},
        {"op": "eq", "left": {"side": "source", "column": "s"}, "right": {"literal": "x"}},
    ]})
    assert evaluate(cond, source=src) is Tri.NULL

    # NULL OR TRUE -> TRUE；NULL OR FALSE -> NULL；FALSE AND NULL -> FALSE
    null_eq = {"op": "eq", "left": {"side": "source", "column": "s"},
               "right": {"literal": "x"}}
    true_pred = {"op": "eq", "left": {"side": "source", "column": "n"},
                 "right": {"literal": 10}}
    false_pred = {"op": "eq", "left": {"side": "source", "column": "n"},
                  "right": {"literal": 0}}
    assert evaluate(P({"any": [null_eq, true_pred]}), source=src) is Tri.TRUE
    assert evaluate(P({"any": [null_eq, false_pred]}), source=src) is Tri.NULL
    assert evaluate(P({"all": [false_pred, null_eq]}), source=src) is Tri.FALSE
    assert evaluate(P({"not": null_eq}), source=src) is Tri.NULL


def test_cross_type_total_order_never_raises():
    # 数字 < 文本 < 字节串；与 None 比较恒为 NULL
    cond = P({"op": "lt", "left": {"side": "source", "column": "s"},
              "right": {"literal": 1}})
    # 文本 vs 数字：数字秩更低，"x" < 1 为 FALSE
    assert evaluate(cond, source={"s": "x"}) is Tri.FALSE
    null_cond = P({"op": "eq", "left": {"side": "source", "column": "s"},
                   "right": {"literal": "x"}})
    assert evaluate(null_cond, source={"s": None}) is Tri.NULL


def test_is_null_predicates():
    cond = P({"op": "is_null", "arg": {"side": "source", "column": "s"}})
    assert evaluate(cond, source={"s": None}) is Tri.TRUE
    assert evaluate(cond, source={"s": ""}) is Tri.FALSE


def test_insert_cannot_reference_target():
    with pytest.raises(ConfigError) as ei:
        P({"op": "eq", "left": {"side": "target", "column": "t"},
           "right": {"literal": 1}}, scope=INSERT_SCOPE)
    assert ei.value.code == "CONFIG_INVALID"
    assert "target" in ei.value.args[0]


def test_delete_cannot_reference_source():
    with pytest.raises(ConfigError):
        P({"op": "eq", "left": {"side": "source", "column": "s"},
           "right": {"literal": 1}}, scope=DELETE_SCOPE)


def test_unknown_column_and_op():
    with pytest.raises(ConfigError) as ei:
        P({"op": "eq", "left": {"side": "source", "column": "ghost"},
           "right": {"literal": 1}})
    assert ei.value.details["column"] == "ghost"
    with pytest.raises(ConfigError):
        P({"op": "regex", "left": {"side": "source", "column": "s"},
           "right": {"literal": ".*"}})


def test_update_can_compare_source_to_target():
    cond = P({"op": "gt", "left": {"side": "source", "column": "n"},
              "right": {"side": "target", "column": "n"}})
    assert evaluate(cond, source={"n": 5}, target={"n": 4}) is Tri.TRUE
    assert evaluate(cond, source={"n": 4}, target={"n": 4}) is Tri.FALSE
