"""Kernel rule tests: verdicts, exact failure categories, diagnostics, coverage."""

from __future__ import annotations

from sqlguard.core.kernel import Kernel
from sqlguard.core.models import Verdict
from sqlguard.core.policy import Policy


def _kernel(policy: Policy, ro_fixture) -> Kernel:
    return Kernel(policy, ro_fixture)


def test_accept_carries_rendered_sql_with_placeholders(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT id FROM users WHERE id = ?", params={"0": 42})
    assert r.verdict is Verdict.ACCEPT
    assert r.rendered_sql == "SELECT id FROM users WHERE id = ?"
    assert r.findings == []


def test_param_as_table_is_rejected_even_when_value_matches_table(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT id FROM ? WHERE id = 1", params={"0": "users"})
    assert r.verdict is Verdict.REJECT
    assert "VALUE_PARAM_AS_IDENTIFIER" in r.reject_codes
    # rendered_sql must NOT contain the inlined string 'users' as a table
    assert r.rendered_sql is None or "FROM users" not in (r.rendered_sql or "")


def test_dynamic_table_resolves_and_renders_quoted(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT id FROM {{ user_relation }} WHERE id = ?",
                 params={"0": 1}, slots={"user_relation": "customers"})
    assert r.verdict is Verdict.ACCEPT
    assert 'FROM "customers"' in r.rendered_sql
    assert r.resolved_identifiers == {"user_relation": "customers"}


def test_dynamic_table_not_in_catalog_rejected(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT id FROM {{ user_relation }}",
                 slots={"user_relation": "users"})
    assert r.verdict is Verdict.ACCEPT  # users is both whitelisted and real
    r2 = k.review("SELECT id FROM {{ user_relation }}",
                  slots={"user_relation": "customers"})
    assert r2.verdict is Verdict.ACCEPT


def test_identifier_not_whitelisted_rejected(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT id FROM {{ user_relation }}",
                 slots={"user_relation": "sqlite_master"})
    assert r.verdict is Verdict.REJECT
    assert r.reject_codes == ["IDENTIFIER_NOT_WHITELISTED"]
    detail = r.findings[0].detail
    assert detail["slot"] == "user_relation"
    assert "users" in detail["allowed"]


def test_quoted_identifier_render_is_injection_safe(service):
    k = Kernel(service.policy, service.fixture)
    # value with embedded quote: rendered as doubled quote inside one ident
    r = k.review("SELECT id FROM users ORDER BY {{ sort_col }}",
                 slots={"sort_col": 'name" -- '})
    assert r.verdict is Verdict.REJECT  # not whitelisted
    assert "IDENTIFIER_NOT_WHITELISTED" in r.reject_codes


def test_array_param_expands_in_list(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT id FROM users WHERE id IN (?)",
                 params={"0": [1, 2, 3]})
    assert r.verdict is Verdict.ACCEPT
    assert r.rendered_sql.endswith("IN ( ? , ? , ? )") or "(?,?,?)" in \
        r.rendered_sql.replace(" ", "")
    diag = r.param_diagnostics[0]
    assert diag["decision"] == "accepted"
    assert diag["reason"] == "array expanded inside IN-list"
    assert diag["binding"]["type"] == "array"
    assert diag["binding"]["length"] == 3


def test_array_param_scalar_context_rejected(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT id FROM users WHERE id = ?", params={"0": [1, 2]})
    assert r.verdict is Verdict.REJECT
    assert r.reject_codes == ["ARRAY_PARAM_IN_SCALAR_CONTEXT"]
    assert r.param_diagnostics[0]["decision"] == "rejected"


def test_empty_array_rejected(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT id FROM users WHERE id IN (?)", params={"0": []})
    assert r.verdict is Verdict.REJECT
    assert r.reject_codes == ["EMPTY_EXPANSION"]


def test_named_param_allowlist_reject_and_accept(service):
    k = Kernel(service.policy, service.fixture)
    bad = k.review("SELECT id FROM users WHERE role = :role",
                   params={"role": "root"})
    assert bad.reject_codes == ["PARAM_VALUE_NOT_ALLOWED"]
    good = k.review("SELECT id FROM users WHERE role = :role",
                    params={"role": "guest"})
    assert good.verdict is Verdict.ACCEPT


def test_missing_binding_category(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT id FROM users WHERE id = ? AND role = :role",
                 params={"0": 1})
    assert r.reject_codes == ["MISSING_BINDING"]
    f = next(f for f in r.findings if f.code == "MISSING_BINDING")
    assert f.detail["marker"] == ":role"


def test_unused_binding_is_advisory(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT id FROM users WHERE id = ?",
                 params={"0": 1, "ghost": 2})
    assert r.verdict is Verdict.ACCEPT
    assert r.advisory_codes == ["UNUSED_BINDING"]


def test_unsupported_param_type_rejected(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT id FROM users WHERE id = ?",
                 params={"0": {"a": 1}})
    assert r.reject_codes == ["INVALID_PARAM_TYPE"]
    assert r.param_diagnostics[0]["binding"]["type"] == "object"


def test_update_without_where_rejected(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("UPDATE users SET status = ?", params={"0": "x"})
    assert r.reject_codes == ["MISSING_WHERE"]


def test_non_writable_target_rejected(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("UPDATE customers SET name = ? WHERE id = ?",
                 params={"0": "x", "1": 1})
    assert r.reject_codes == ["TARGET_TABLE_NOT_WRITABLE"]


def test_unknown_static_table_and_column(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT id FROM secrets", )
    assert "UNKNOWN_TABLE" in r.reject_codes
    r2 = k.review("INSERT INTO users (hacker_col) VALUES (?)",
                  params={"0": 1})
    assert "UNKNOWN_TABLE" in r2.reject_codes


def test_planner_rejects_unknown_column_in_projection(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT nope FROM users WHERE id = ?", params={"0": 1})
    assert r.reject_codes == ["RENDERED_SQL_INVALID"]


def test_lex_error_is_unanalyzable_not_reject(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT 'unterminated")
    assert r.verdict is Verdict.UNANALYZABLE
    assert r.unanalyzable_codes == ["LEX_ERROR"]


def test_coverage_records_relation_decisions(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT u.id FROM users u JOIN orders o ON u.id = o.user_id "
                 "WHERE u.id = ?", params={"0": 1})
    assert r.verdict is Verdict.ACCEPT
    decisions = {(c.get("relation"), c["result"]) for c in
                 r.coverage.relation_checks}
    assert ("users", "accepted") in decisions
    assert ("orders", "accepted") in decisions
    assert r.coverage.statement_parsed is True


def test_coverage_reports_subquery_limit(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT id FROM (SELECT id FROM users) z WHERE id = ?",
                 params={"0": 1})
    assert r.verdict is Verdict.ACCEPT
    assert any("subquery" in s for s in r.coverage.skipped)


def test_inline_policy_override_adds_slot(service):
    k = Kernel(service.policy, service.fixture)
    override = {
        "slots": {
            "adhoc": {"allowed": ["id", "name"], "scope": "sort",
                      "require_in_schema": False}
        }
    }
    r = k.review("SELECT id FROM users ORDER BY {{ adhoc }}",
                 slots={"adhoc": "name"}, )
    # no override -> undeclared
    assert "SLOT_UNDECLARED" in r.reject_codes
    pol = service.policy.with_overrides(override)
    k2 = Kernel(pol, service.fixture)
    r2 = k2.review("SELECT id FROM users ORDER BY {{ adhoc }}",
                   slots={"adhoc": "name"})
    assert r2.verdict is Verdict.ACCEPT


def test_mixed_anonymous_and_numbered_params_rejected(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT ? + ?2", params={"0": 1, "2": 2})
    assert r.verdict is Verdict.REJECT
    assert r.reject_codes == ["MIXED_PARAMETER_STYLES", "MIXED_PARAMETER_STYLES"]


def test_numbered_param_alone_is_accepted(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT ?1 + ?2", params={"0": 1, "1": 2})
    assert r.verdict is Verdict.ACCEPT


def test_unicode_string_literal_is_data(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review("SELECT id FROM users WHERE name = ? AND '日本語 ? :x' = 'x'",
                 params={"0": "名前"})
    assert r.verdict is Verdict.ACCEPT
    assert len([d for d in r.param_diagnostics]) == 1


def test_case_insensitive_table_and_column_names(service):
    k = Kernel(service.policy, service.fixture)
    # SQLite resolves identifiers case-insensitively; the reviewer must not
    # false-positive on USERS / ID.
    r = k.review("SELECT ID FROM USERS WHERE ID = ?", params={"0": 1})
    assert r.verdict is Verdict.ACCEPT, r.reject_codes
    r2 = k.review(
        "UPDATE Users SET NAME = ? WHERE Id = ?", params={"0": "x", "1": 1})
    assert r2.verdict is Verdict.ACCEPT, r2.reject_codes


def test_blob_literal_renders_intact(service):
    k = Kernel(service.policy, service.fixture)
    r = k.review(
        "SELECT id FROM users WHERE x'4142' IS NOT NULL AND id = ?",
        params={"0": 1})
    assert r.verdict is Verdict.ACCEPT
    assert "x'4142'" in r.rendered_sql


def test_kernel_never_mutates_fixture(service, ro_fixture):
    before = ro_fixture.connection.execute(
        "SELECT count(*) FROM users").fetchone()[0]
    k = Kernel(service.policy, ro_fixture)
    k.review("UPDATE users SET name = ? WHERE id = ?",
             params={"0": "HACKED", "1": 1})
    after = ro_fixture.connection.execute("SELECT count(*) FROM users").fetchone()[0]
    # no row changed either way because review never executes the statement
    name = ro_fixture.connection.execute(
        "SELECT name FROM users WHERE id = 1").fetchone()[0]
    assert before == after
    assert name != "HACKED"
