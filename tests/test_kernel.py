"""Kernel unit tests asserting concrete verdicts and failure categories."""

from __future__ import annotations


def review(kernel, sql, **kw):
    return kernel.review(sql, **kw)


def codes(result, severity="error"):
    return [f.code for f in result.findings if f.severity == severity]


# --- accept paths ---------------------------------------------------------

def test_accept_named_equality(kernel):
    r = review(kernel, "SELECT id, email FROM users WHERE id = :id",
               parameters={"id": 1})
    assert r.verdict == "accept"
    assert codes(r) == []
    assert r.basis["policy_id"] == "shop-readonly-v1"
    assert r.diagnostics["decision"] == "accept"


def test_accept_array_binding_for_in(kernel):
    r = review(kernel, "SELECT id FROM orders WHERE id IN (?)",
               parameters=[[1, 2, 3]])
    assert r.verdict == "accept"
    assert r.bound_parameters[0]["position"] == "array"
    assert r.bound_parameters[0]["kind"] == "array"
    assert r.bound_parameters[0]["length"] == 3


def test_accept_dynamic_order_slot(kernel):
    r = review(kernel,
               "SELECT id FROM orders ORDER BY ${sort_col} ${dir_kw}",
               identifiers={"sort_col": "created_at", "dir_kw": "DESC"})
    assert r.verdict == "accept"
    roles = {b["slot"]: b["role"] for b in r.identifier_bindings}
    assert roles == {"sort_col": "column", "dir_kw": "keyword"}


def test_accept_slot_table_with_alias_and_qualified_columns(kernel):
    r = review(kernel,
               "SELECT t.id FROM ${tbl} t WHERE t.stock > :n",
               parameters={"n": 10}, identifiers={"tbl": "products"})
    assert r.verdict == "accept"
    assert r.identifier_bindings[0]["bound"] == "products"


def test_null_binding_is_a_valid_scalar(kernel):
    r = review(kernel,
               "UPDATE users SET display_name = :n WHERE id = :id",
               parameters={"n": None, "id": 1})
    assert r.verdict == "accept"
    assert r.bound_parameters[0]["kind"] == "null"


# --- value-as-identifier rejection ---------------------------------------

def test_value_param_in_table_position_rejected(kernel):
    r = review(kernel, "SELECT id FROM ?", parameters=["orders"])
    assert r.verdict == "reject"
    assert "VALUE_USED_AS_IDENTIFIER" in codes(r)


def test_value_param_as_order_key_rejected_even_for_integer(kernel):
    r = review(kernel, "SELECT id FROM orders ORDER BY ?", parameters=[2])
    assert r.verdict == "reject"
    assert "VALUE_USED_AS_IDENTIFIER" in codes(r)


# --- identifier slot rules -----------------------------------------------

def test_undeclared_slot_rejected(kernel):
    r = review(kernel, "SELECT id FROM orders ORDER BY ${x}",
               identifiers={"x": "id"})
    assert r.verdict == "reject"
    assert "IDENTIFIER_SLOT_NOT_DECLARED" in codes(r)


def test_unbound_slot_rejected(kernel):
    r = review(kernel, "SELECT id FROM orders ORDER BY ${sort_col}")
    assert r.verdict == "reject"
    assert "IDENTIFIER_SLOT_UNBOUND" in codes(r)


def test_slot_payload_with_semicolon_rejected(kernel):
    r = review(kernel, "SELECT id FROM ${tbl}",
               identifiers={"tbl": "orders; DROP TABLE users"})
    assert r.verdict == "reject"
    assert "IDENTIFIER_NOT_ALLOWED" in codes(r)


def test_slot_payload_with_quote_rejected(kernel):
    r = review(kernel, "SELECT id FROM ${tbl}",
               identifiers={"tbl": '"orders"'})
    assert r.verdict == "reject"
    assert "IDENTIFIER_NOT_ALLOWED" in codes(r)


def test_slot_payload_with_whitespace_rejected(kernel):
    r = review(kernel, "SELECT id FROM ${tbl}",
               identifiers={"tbl": " orders "})
    assert r.verdict == "reject"


def test_sort_column_cross_table_not_allowed(kernel):
    # 'sku' exists on products but the query scope is orders
    r = review(kernel, "SELECT id FROM orders ORDER BY ${sort_col}",
               identifiers={"sort_col": "sku"})
    assert r.verdict == "reject"
    assert "IDENTIFIER_NOT_ALLOWED" in codes(r)


def test_slot_used_with_wrong_role_rejected(kernel):
    # tbl is a table-role slot; using it as a column must fail role check
    r = review(kernel, "SELECT ${tbl} FROM orders",
               identifiers={"tbl": "id"})
    assert r.verdict == "reject"
    assert "SLOT_ROLE_MISMATCH" in codes(r)


def test_keyword_slot_rejects_value_outside_keyword_set(kernel):
    r = review(kernel,
               "SELECT id FROM orders ORDER BY ${sort_col} ${dir_kw}",
               identifiers={"sort_col": "id", "dir_kw": "SIDEWAYS"})
    assert r.verdict == "reject"
    assert "IDENTIFIER_NOT_ALLOWED" in codes(r)


# --- binding type rules ---------------------------------------------------

def test_missing_named_binding_rejected(kernel):
    r = review(kernel, "SELECT id FROM orders WHERE id = :id")
    assert r.verdict == "reject"
    assert "PARAMETER_UNBOUND" in codes(r)


def test_scalar_bound_where_array_expected_rejected(kernel):
    r = review(kernel, "SELECT id FROM orders WHERE id IN (?)",
               parameters=[1])
    assert r.verdict == "reject"
    assert "PARAMETER_TYPE_INVALID" in codes(r)


def test_empty_array_rejected(kernel):
    r = review(kernel, "SELECT id FROM orders WHERE id IN (?)",
               parameters=[[]])
    assert r.verdict == "reject"
    assert "ARRAY_EMPTY" in codes(r)


def test_nested_array_element_rejected(kernel):
    r = review(kernel, "SELECT id FROM orders WHERE id IN (?)",
               parameters=[[1, [2]]])
    assert r.verdict == "reject"
    assert "ARRAY_ELEMENT_INVALID" in codes(r)


def test_dict_element_in_array_rejected(kernel):
    r = review(kernel, "SELECT id FROM orders WHERE id IN (?)",
               parameters=[[{"a": 1}]])
    assert r.verdict == "reject"
    assert "ARRAY_ELEMENT_INVALID" in codes(r)


def test_limit_negative_rejected(kernel):
    r = review(kernel, "SELECT id FROM users LIMIT :n",
               parameters={"n": -1})
    assert r.verdict == "reject"
    assert "LIMIT_VALUE_INVALID" in codes(r)


def test_limit_string_rejected(kernel):
    r = review(kernel, "SELECT id FROM users LIMIT :n",
               parameters={"n": "all"})
    assert r.verdict == "reject"
    assert "LIMIT_VALUE_INVALID" in codes(r)


def test_limit_boolean_rejected(kernel):
    r = review(kernel, "SELECT id FROM users LIMIT :n",
               parameters={"n": True})
    assert r.verdict == "reject"


# --- whitelist / schema ---------------------------------------------------

def test_unknown_table_rejected(kernel):
    r = review(kernel, "SELECT * FROM nope")
    assert r.verdict == "reject"
    assert "TABLE_NOT_WHITELISTED" in codes(r)


def test_unknown_column_rejected(kernel):
    r = review(kernel, "SELECT password FROM users")
    assert r.verdict == "reject"
    assert "COLUMN_UNRESOLVED" in codes(r)


def test_delete_on_audit_events_forbidden(kernel):
    r = review(kernel, "DELETE FROM audit_events WHERE id = :id",
               parameters={"id": 1})
    assert r.verdict == "reject"
    assert "TABLE_OP_NOT_ALLOWED" in codes(r)


def test_ambiguous_unqualified_column_rejected(kernel):
    # 'id' exists on both joined tables
    r = review(kernel,
               "SELECT id FROM orders JOIN users ON orders.user_id = users.id")
    assert r.verdict == "reject"
    assert "AMBIGUOUS_IDENTIFIER" in codes(r)


# --- unparseable input ----------------------------------------------------

def test_unterminated_string_is_unanalyzable(kernel):
    r = review(kernel, "SELECT 'abc")
    assert r.verdict == "unanalyzable"
    assert codes(r) == ["LEX_ERROR"]
    assert r.diagnostics["at_offset"] is not None


def test_stacked_query_is_unanalyzable(kernel):
    r = review(kernel, "SELECT 1; DROP TABLE users")
    assert r.verdict == "unanalyzable"
    assert codes(r) == ["UNSUPPORTED_SYNTAX"]


def test_subquery_is_unanalyzable(kernel):
    r = review(kernel,
               "SELECT * FROM users WHERE id IN (SELECT user_id FROM orders)")
    assert r.verdict == "unanalyzable"
    assert codes(r) == ["UNSUPPORTED_SYNTAX"]


# --- evidence + diagnostics ----------------------------------------------

def test_inert_occurrences_recorded_as_evidence(kernel):
    sql = "SELECT id FROM users -- :x ?\nWHERE id = :id AND email = 'a @b'"
    r = review(kernel, sql, parameters={"id": 1})
    assert r.verdict == "accept"
    inert = {o["text"] for o in r.inert_occurrences}
    assert {":x", "?", "@b"} <= inert
    # inert occurrences must not consume the binding
    assert codes(r) == []


def test_unused_binding_is_warning_not_rejection(kernel):
    r = review(kernel, "SELECT id FROM orders WHERE id = :id",
               parameters={"id": 1, "extra": 2})
    assert r.verdict == "accept"
    assert codes(r, "warning") == ["BINDING_UNUSED"]


def test_request_id_propagates_and_diagnostics_reference_it(kernel):
    r = review(kernel, "SELECT id FROM orders WHERE id = :id",
               parameters={"id": 1}, request_id="fixed-id")
    assert r.request_id == "fixed-id"
    assert r.diagnostics["request_id"] == "fixed-id"


def test_sensitive_value_is_redacted_in_evidence(kernel):
    r = review(kernel,
               "UPDATE users SET email = :password WHERE id = :id",
               parameters={"password": "supersecret-value", "id": 1})
    pw = next(p for p in r.bound_parameters if p["ref"] == "password")
    assert pw.get("redacted") is True
    assert "preview" not in pw
    assert "fingerprint" in pw
    assert "supersecret-value" not in str(r.to_dict())


def test_long_string_preview_is_omitted(kernel):
    long = "x" * 100
    r = review(kernel, "SELECT id FROM users WHERE display_name = :v",
               parameters={"v": long})
    ev = r.bound_parameters[0]
    assert "preview" not in ev and ev["length"] == 100


def test_basis_reports_schema_digest(kernel, schema):
    r = review(kernel, "SELECT id FROM orders WHERE id = :id",
               parameters={"id": 1})
    assert r.basis["schema_digest"] == schema.digest
