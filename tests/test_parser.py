"""Parser structural tests — exact contexts, failure categories and positions."""

from __future__ import annotations

from sqlguard.core.parser import Parser


def parse(sql: str):
    r = Parser().parse(sql)
    return r.statements[0] if r.statements else None, r.findings


def test_quote_escaped_placeholder_not_recorded_as_param():
    stmt, findings = parse("SELECT id FROM users WHERE name = '?' AND id = ?")
    assert len(stmt.params) == 1
    assert stmt.params[0].marker == "?"
    assert stmt.params[0].context == "where"


def test_line_comment_placeholder_inert():
    stmt, _ = parse("SELECT ? -- :x ?5\n FROM t")
    assert [p.marker for p in stmt.params] == ["?"]


def test_block_comment_placeholder_inert():
    stmt, _ = parse("SELECT ? /* ? :name @a $b */ FROM t")
    assert [p.marker for p in stmt.params] == ["?"]


def test_array_context_marked_only_inside_in_list():
    stmt, _ = parse("SELECT * FROM t WHERE a = ? AND b IN (?, ?) AND c IN (SELECT x)")
    contexts = [(p.context, p.expansion) for p in stmt.params]
    assert contexts == [("where", False), ("where", True), ("where", True)]


def test_in_subquery_param_is_scalar_context_not_expansion():
    stmt, _ = parse("SELECT * FROM t WHERE id IN (SELECT id FROM t WHERE x = ?)")
    assert len(stmt.params) == 1
    assert stmt.params[0].expansion is False


def test_dynamic_sort_field_is_slot_not_param():
    stmt, findings = parse("SELECT id FROM t ORDER BY {{ sort_col }} DESC")
    assert [(s.name, s.context) for s in stmt.slots] == [("sort_col", "order")]
    assert all(f.code != "VALUE_PARAM_AS_IDENTIFIER" for f in findings)


def test_order_by_direct_question_mark_is_identifier_abuse():
    stmt, findings = parse("SELECT id FROM t ORDER BY ?")
    codes = [f.code for f in findings]
    assert "VALUE_PARAM_AS_IDENTIFIER" in codes
    assert stmt.params[0].context == "order"


def test_group_by_direct_question_mark_is_identifier_abuse():
    stmt, findings = parse("SELECT count(*) FROM t GROUP BY ?")
    assert "VALUE_PARAM_AS_IDENTIFIER" in {f.code for f in findings}


def test_param_as_table_recorded_with_span():
    stmt, findings = parse("SELECT * FROM ?")
    assert "VALUE_PARAM_AS_IDENTIFIER" in {f.code for f in findings}
    assert stmt.relations[0].kind == "param"
    assert stmt.relations[0].span.start >= 0


def test_slot_as_table_relation():
    stmt, findings = parse("SELECT * FROM {{ user_relation }}")
    assert stmt.relations[0].slot_name == "user_relation"
    assert not findings


def test_insert_columns_collected():
    stmt, findings = parse("INSERT INTO users (name, email) VALUES (?, ?)")
    assert [c.name for c in stmt.insert_columns] == ["name", "email"]
    assert len(stmt.params) == 2
    assert all(p.context == "values" for p in stmt.params)
    assert stmt.target.name == "users"
    assert not findings


def test_update_set_lhs_columns_collected_rhs_values():
    stmt, findings = parse("UPDATE users SET name = ?, email = ? WHERE id = ?")
    assert [c.name for c in stmt.set_columns] == ["name", "email"]
    rhs = [p for p in stmt.params if p.context == "set_rhs"]
    where = [p for p in stmt.params if p.context == "where"]
    assert len(rhs) == 2 and len(where) == 1
    assert stmt.has_where is True


def test_delete_where_detection():
    stmt, _ = parse("DELETE FROM users WHERE id = ?")
    assert stmt.has_where is True
    stmt2, findings2 = parse("DELETE FROM users")
    assert stmt2.has_where is False


def test_multiple_statements_flag():
    stmt, findings = parse("SELECT 1; DELETE FROM users")
    codes = {f.code for f in findings}
    assert "MULTIPLE_STATEMENTS" in codes
    assert "STATEMENT_TYPE_NOT_ALLOWED" in codes


def test_trailing_semicolon_alone_is_allowed():
    stmt, findings = parse("SELECT 1;")
    assert stmt is not None
    assert not findings


def test_ddl_is_not_an_allowed_statement():
    stmt, findings = parse("ALTER TABLE users ADD COLUMN x TEXT")
    assert "STATEMENT_TYPE_NOT_ALLOWED" in {f.code for f in findings}


def test_empty_and_comment_only_templates():
    for sql in ("", "   ", "-- nothing\n", "/* x */"):
        stmt, findings = parse(sql)
        assert stmt is None
        assert "EMPTY_TEMPLATE" in {f.code for f in findings}


def test_unterminated_string_is_lex_error_finding():
    stmt, findings = parse("SELECT 'abc")
    assert stmt is None
    assert findings[0].code == "LEX_ERROR"
    assert findings[0].span is not None


def test_join_relations_all_collected():
    stmt, _ = parse(
        "SELECT * FROM users u JOIN orders o ON u.id = o.user_id "
        "LEFT JOIN customers c ON c.id = u.id WHERE u.id = ?")
    names = {r.name for r in stmt.relations}
    assert names == {"users", "orders", "customers"}
    assert stmt.params[0].context == "where"


def test_compound_select():
    stmt, _ = parse("SELECT id FROM a UNION SELECT id FROM b ORDER BY 1")
    assert stmt.compound is True
    assert {r.name for r in stmt.relations} == {"a", "b"}


def test_cte_uses_merged_into_main_statement():
    stmt, findings = parse(
        "WITH r AS (SELECT id FROM users WHERE id > ?) SELECT * FROM r WHERE id = ?")
    assert len(stmt.params) == 2
    cte_rel = next(r for r in stmt.relations if r.kind == "cte")
    assert cte_rel.name == "r"


def test_named_parameter_shapes():
    stmt, _ = parse("SELECT * FROM t WHERE a = :a AND b = @b AND c = $c AND d = ?3")
    assert [p.marker for p in stmt.params] == [":a", "@b", "$c", "?3"]


def test_string_with_classic_payload_is_one_literal():
    stmt, findings = parse("SELECT * FROM users WHERE name = 'x'' OR ''1''=''1'")
    assert stmt.params == []
    assert not findings


def test_parser_finding_has_location_context():
    _, findings = parse("SELECT * FROM ?")
    f = next(f for f in findings if f.code == "VALUE_PARAM_AS_IDENTIFIER")
    assert f.context == "from"
    assert f.span.line == 0
