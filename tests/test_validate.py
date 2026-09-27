"""Validation tests: whitelist, type checks, complexity budget.

All checks run on the parsed tree before normalization; every failure
category is asserted by its stable code.
"""

from __future__ import annotations

import pytest

from searchdsl.config import Limits
from searchdsl.errors import ValidationError
from searchdsl.parser import parse
from searchdsl.validate import measure, validate

LIMITS = Limits()


def v(query, schema):
    return validate(parse(query), schema, LIMITS)


def test_unknown_field_rejected(schema):
    with pytest.raises(ValidationError) as ei:
        v("ghost:cat", schema)
    assert ei.value.code == "FIELD_UNKNOWN"
    assert ei.value.pos.start == 0


def test_unknown_field_inside_or_still_rejected(schema):
    # Normalization could simplify this; running pre-normalize keeps the error.
    with pytest.raises(ValidationError) as ei:
        v("ghost:x OR title:fox", schema)
    assert ei.value.code == "FIELD_UNKNOWN"


def test_unknown_field_inside_not_still_rejected(schema):
    with pytest.raises(ValidationError) as ei:
        v("NOT ghost:x", schema)
    assert ei.value.code == "FIELD_UNKNOWN"


def test_phrase_on_int_rejected(schema):
    with pytest.raises(ValidationError) as ei:
        v('year:"happy new year"', schema)
    assert ei.value.code == "FIELD_TYPE_MISMATCH"


def test_range_on_keyword_rejected(schema):
    with pytest.raises(ValidationError) as ei:
        v("category:[a TO z]", schema)
    assert ei.value.code == "FIELD_TYPE_MISMATCH"


def test_range_on_text_rejected(schema):
    with pytest.raises(ValidationError) as ei:
        v("title:[a TO z]", schema)
    assert ei.value.code == "FIELD_TYPE_MISMATCH"


def test_unfielded_range_rejected(schema):
    with pytest.raises(ValidationError) as ei:
        v("[1 TO 5]", schema)
    assert ei.value.code == "FIELD_TYPE_MISMATCH"


def test_int_term_must_be_integer(schema):
    with pytest.raises(ValidationError) as ei:
        v("year:banana", schema)
    assert ei.value.code == "VALUE_MALFORMED"


def test_int_range_bounds_must_be_integer(schema):
    with pytest.raises(ValidationError) as ei:
        v("year:[2000 TO banana]", schema)
    assert ei.value.code == "VALUE_MALFORMED"


def test_date_term_must_be_iso(schema):
    with pytest.raises(ValidationError) as ei:
        v("published:2020/01/01", schema)
    assert ei.value.code == "VALUE_MALFORMED"


def test_date_term_rejects_impossible_calendar_date(schema):
    with pytest.raises(ValidationError) as ei:
        v("published:2021-02-29", schema)
    assert ei.value.code == "VALUE_MALFORMED"


def test_inverted_range_is_empty(schema):
    with pytest.raises(ValidationError) as ei:
        v("year:[2010 TO 2000]", schema)
    assert ei.value.code == "RANGE_EMPTY"


def test_exclusive_equal_bounds_is_empty(schema):
    with pytest.raises(ValidationError) as ei:
        v("year:{2010 TO 2010}", schema)
    assert ei.value.code == "RANGE_EMPTY"


def test_inclusive_equal_bounds_is_valid(schema):
    report = v("year:[2010 TO 2010]", schema)
    assert report.clauses == 1


def test_text_punctuation_only_is_malformed(schema):
    with pytest.raises(ValidationError) as ei:
        v("title:_", schema)
    assert ei.value.code == "VALUE_MALFORMED"

    with pytest.raises(ValidationError) as ei:
        v("title:...", schema)
    assert ei.value.code == "VALUE_MALFORMED"


def test_budget_depth(schema):
    # depth: NOT NOT NOT a -> 4
    with pytest.raises(ValidationError) as ei:
        validate(parse("NOT NOT NOT a"), schema, Limits(max_nesting_depth=3))
    assert ei.value.code == "BUDGET_DEPTH"
    assert ei.value.detail["depth"] == 4


def test_budget_clauses(schema):
    with pytest.raises(ValidationError) as ei:
        validate(parse("a b c d e"), schema, Limits(max_clauses=4))
    assert ei.value.code == "BUDGET_CLAUSES"
    assert ei.value.detail["clauses"] == 5


def test_budget_query_terms(schema):
    # 'a b c d e' analyzes to 5 terms.
    with pytest.raises(ValidationError) as ei:
        validate(parse("a b c d e"), schema, Limits(max_query_terms=4))
    assert ei.value.code == "BUDGET_QUERY_TERMS"


def test_budget_phrase_terms(schema):
    with pytest.raises(ValidationError) as ei:
        validate(parse('"one two three four"'), schema, Limits(max_phrase_terms=3))
    assert ei.value.code == "BUDGET_PHRASE_TERMS"


def test_measure_reports_dimensions(schema):
    rep = measure(parse("a AND (b OR c)"))
    assert rep.clauses == 3
    assert rep.depth == 3
    assert rep.query_terms == 3


def test_valid_queries_pass(schema):
    for q in [
        "title:fox",
        "tags:dog",
        "year:2010",
        "published:2020-02-29",
        "year:[2000 TO 2010]",
        "published:{1999-01-01 TO 2020-12-31}",
        'body:"quick brown fox"',
    ]:
        assert v(q, schema).clauses >= 1
