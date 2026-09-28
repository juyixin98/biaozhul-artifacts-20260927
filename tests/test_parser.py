"""Parser unit tests: AST shape for the supported subset and precise
failures for anything outside it."""

from __future__ import annotations

import pytest

from sqlguard import ast_nodes as ast
from sqlguard.parser import parse, ParseError, UnsupportedSyntax


def _only(sql: str):
    s = parse(sql)
    assert len(s.statements) == 1
    return s.statements[0]


def test_select_columns_and_from():
    st = _only("SELECT id, email FROM users")
    assert isinstance(st, ast.Select)
    assert [expr for expr, _ in st.items] != []
    assert st.from_tables[0].name == "users"


def test_where_predicate_is_binary_ast():
    st = _only("SELECT id FROM orders WHERE id = :id AND status = @s")
    assert isinstance(st.where, ast.Binary)
    assert st.where.op == "AND"


def test_in_list_marks_single_placeholder_as_array_context():
    st = _only("SELECT id FROM orders WHERE id IN (?)")
    assert isinstance(st.where, ast.InList)
    p = st.where.items[0]
    assert isinstance(p, ast.Param) and p.array_context is True


def test_explicit_in_list_keeps_scalar_context():
    st = _only("SELECT id FROM orders WHERE id IN (?, ?)")
    assert all(isinstance(i, ast.Param) and not i.array_context
               for i in st.where.items)


def test_order_by_direction_slot():
    st = _only("SELECT id FROM orders ORDER BY id ${d}")
    oi = st.order_by[0]
    assert oi.direction_slot is not None
    assert oi.direction_slot.name == "d"


def test_table_slot_in_from():
    st = _only("SELECT id FROM ${tbl} t")
    ref = st.from_tables[0]
    assert ref.slot is not None and ref.slot.name == "tbl"
    assert ref.alias == "T"


def test_value_param_in_table_position_parses_for_kernel_rejection():
    st = _only("SELECT id FROM ?")
    assert st.from_tables[0].param is not None


def test_qualified_column_slot():
    st = _only("SELECT id FROM orders o ORDER BY o.${c}")
    slot = st.order_by[0].expr
    assert isinstance(slot, ast.Slot) and slot.qualifier == "O"


def test_limit_placeholder_carries_limit_context():
    st = _only("SELECT id FROM users LIMIT :n")
    assert isinstance(st.limit, ast.Param) and st.limit.limit_context is True


def test_insert_rows_and_columns():
    st = _only("INSERT INTO users (id, email) VALUES (?, ?)")
    assert isinstance(st, ast.Insert)
    assert [c.name for c in st.columns] == ["id", "email"]
    assert len(st.rows[0]) == 2


def test_update_assignment_pairs():
    st = _only("UPDATE orders SET status = :s WHERE id = :id")
    assert isinstance(st, ast.Update)
    assert st.assignments[0][0].name == "status"


def test_trailing_semicolon_is_one_statement():
    assert len(parse("SELECT 1;").statements) == 1


def test_stacked_statements_rejected():
    with pytest.raises(UnsupportedSyntax) as exc:
        parse("SELECT 1; DROP TABLE x")
    assert "multiple semicolon-separated" in exc.value.message


def test_subquery_unsupported():
    with pytest.raises(UnsupportedSyntax):
        parse("SELECT * FROM users WHERE id IN (SELECT user_id FROM orders)")


def test_cte_unsupported():
    with pytest.raises(UnsupportedSyntax):
        parse("WITH x AS (SELECT 1) SELECT * FROM x")


def test_union_unsupported():
    with pytest.raises(UnsupportedSyntax):
        parse("SELECT 1 UNION SELECT 2")


def test_drop_is_unsupported_statement_not_parse_garbage():
    with pytest.raises(UnsupportedSyntax):
        parse("DROP TABLE users")


def test_malformed_where_raises_parse_error():
    with pytest.raises(ParseError):
        parse("SELECT id FROM orders WHERE")


def test_empty_statement_is_parse_error():
    with pytest.raises(ParseError):
        parse(";")


def test_comments_are_attached_to_script_not_grammar():
    s = parse("SELECT /* hi */ id FROM x -- tail\n")
    assert len(s.comment_spans) == 2


def test_any_marks_param_array_context():
    st = _only("SELECT id FROM orders WHERE status = ANY(:s)")
    call = st.where.right
    assert isinstance(call, ast.FuncCall) and call.name == "ANY"
    assert call.args[0].array_context is True
