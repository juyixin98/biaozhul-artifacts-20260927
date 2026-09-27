"""Normalizer tests: boolean identities, empty-query semantics, idempotence."""

from __future__ import annotations

from searchdsl.astnodes import (
    And,
    MatchAll,
    MatchNone,
    Not,
    Or,
    Phrase,
    Term,
    canonical_json,
)
from searchdsl.normalize import is_canonical, normalize


def t(v, field=None):
    return Term(value=v, field_name=field)


def n(query_or_node):
    from searchdsl.parser import parse

    node = parse(query_or_node) if isinstance(query_or_node, str) else query_or_node
    return normalize(node)


def test_empty_query_is_match_all_fixed_point():
    # Parser rejects empty text, but MatchAll produced elsewhere must stay.
    m = normalize(MatchAll())
    assert isinstance(m, MatchAll)
    assert canonical_json(m) == '{"op":"match_all"}'


def test_empty_group_stays_match_all():
    assert isinstance(n("()"), MatchAll)


def test_nonexistent_field_is_preserved_through_normalize():
    # Normalize must not erase or rename unknown fields; validation owns that.
    tree = n("ghost:x OR y")
    js = canonical_json(tree)
    assert '"field":"ghost"' in js and '"value":"x"' in js


def test_idempotent_dedup():
    assert canonical_json(n("a a a")) == '{"field":null,"op":"term","value":"a"}'


def test_and_with_complement_is_none():
    assert isinstance(n("a AND NOT a"), MatchNone)


def test_or_with_complement_is_all():
    assert isinstance(n("a OR NOT a"), MatchAll)


def test_absorption_and():
    assert canonical_json(n("a AND (a OR b)")) == '{"field":null,"op":"term","value":"a"}'


def test_absorption_or():
    assert canonical_json(n("a OR (a AND b)")) == '{"field":null,"op":"term","value":"a"}'


def test_and_none_short_circuits_or_group():
    # (a AND NOT a) OR b -> MatchNone OR b -> b
    assert canonical_json(n("(a AND NOT a) OR b")) == '{"field":null,"op":"term","value":"b"}'


def test_or_all_short_circuits_and_group():
    assert isinstance(n("(a OR NOT a) AND b"), MatchAll) is False
    # (match-all) AND b -> b
    assert canonical_json(n("(a OR NOT a) AND b")) == '{"field":null,"op":"term","value":"b"}'


def test_flatten_nested_same_operator():
    tree = n("(a OR b) OR (c OR d)")
    js = canonical_json(tree)
    assert js.count('"op":"or"') == 1
    for v in ("a", "b", "c", "d"):
        assert f'"value":"{v}"' in js


def test_not_not_collapses():
    assert canonical_json(n("NOT NOT a")) == '{"field":null,"op":"term","value":"a"}'


def test_not_match_all_is_none_and_none_negated_is_all():
    assert isinstance(normalize(Not(MatchAll())), MatchNone)
    assert isinstance(normalize(Not(MatchNone())), MatchAll)


def test_empty_phrase_becomes_match_none():
    assert isinstance(normalize(Phrase(value="   ")), MatchNone)


def test_canonical_ordering_is_deterministic():
    assert canonical_json(n("z a m")) == canonical_json(n("m z a"))


def test_idempotence_on_many_shapes():
    queries = [
        "a AND b OR c AND d",
        "NOT (a OR b) AND NOT c",
        "(a OR b OR a) AND (c OR NOT c)",
        "x y z OR (x y z) OR w",
        "NOT NOT (a AND NOT b)",
    ]
    for q in queries:
        once = n(q)
        twice = normalize(once)
        assert canonical_json(once) == canonical_json(twice), q
        assert is_canonical(once), q


def test_not_is_not_distributed_no_clause_explosion():
    # NOT (a OR b) stays a NOT node; it must not become (NOT a AND NOT b).
    tree = n("NOT (a OR b)")
    assert isinstance(tree, Not)
    assert isinstance(tree.child, Or) and len(tree.child.children) == 2


def test_structure_objects_round_trip():
    node = And((Or((t("a"), t("b"))), Not(t("c"))))
    out = normalize(node)
    js = canonical_json(out)
    assert js.startswith('{"children":')
    assert normalize(out).to_canonical() == out.to_canonical()
