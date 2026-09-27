"""Budget enforcement through the public engine facade."""

from __future__ import annotations


def test_depth_budget_engine(small_budget_engine):
    resp = small_budget_engine.search("NOT a AND b")  # deepest path 3, ok
    assert resp.status == "ok"
    resp = small_budget_engine.search("NOT NOT NOT a")  # depth 4 > 3
    assert resp.status == "error"
    assert resp.error["code"] == "BUDGET_DEPTH"
    assert resp.diagnostics["summary"]["status"] == "error"
    assert resp.diagnostics["summary"]["error_code"] == "BUDGET_DEPTH"


def test_clause_budget_engine(small_budget_engine):
    resp = small_budget_engine.search("a b c d e")
    assert resp.status == "error"
    assert resp.error["code"] == "BUDGET_CLAUSES"
    assert resp.error["detail"]["clauses"] == 5
    assert resp.canonical is None


def test_phrase_budget_engine(small_budget_engine):
    resp = small_budget_engine.search('"one two three four"')
    assert resp.status == "error"
    assert resp.error["code"] == "BUDGET_PHRASE_TERMS"


def test_error_never_reported_as_success(small_budget_engine):
    for q in ["", "a AND", "ghost:x", "year:nope", "a b c d e", '"x" ']:
        resp = small_budget_engine.search(q)
        if resp.status == "error":
            assert resp.error is not None and resp.error["code"] != "INTERNAL"
            assert resp.results == []
            assert resp.total == 0


def test_result_window_budget(engine):
    resp = engine.search("fox", limit=10, offset=995)
    assert resp.status == "error"
    assert resp.error["code"] == "BUDGET_RESULT_WINDOW"


def test_query_too_long(engine):
    resp = engine.search("a" * 5000)
    assert resp.status == "error"
    assert resp.error["code"] == "QUERY_TOO_LONG"
