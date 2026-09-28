"""Tests for priority overlap resolution and planning semantics."""

from __future__ import annotations

import pytest

from app.errors import CaptureMissingError, MatchBudgetExceededError
from app.planner import (
    PlannerLimits,
    RuleSpec,
    apply_plan_stream,
    build_plan,
)
from app.textspec import normalize_source


def plan(text, rules, **kw):
    nt = normalize_source(text if isinstance(text, str) else text)
    data = text.encode() if isinstance(text, str) else bytes(text)
    return build_plan(data, rules, **kw)


def apply(text, result):
    data = text.encode() if isinstance(text, str) else bytes(text)
    return apply_plan_stream(result.plan, data).output.decode()


def test_no_matches_gives_empty_plan():
    r = plan("nothing here", [RuleSpec("x", r"z+", "Z")])
    assert r.plan.edit_count == 0
    assert apply("nothing here", r) == "nothing here"


def test_adjacent_matches_both_apply_without_gap():
    r = plan("aaaa", [RuleSpec("a", r"a", "X")])
    assert [(e.start, e.end) for e in r.plan.edits] == [
        (0, 1), (1, 2), (2, 3), (3, 4)
    ]
    assert apply("aaaa", r) == "XXXX"


def test_priority_decides_overlapping_matches(run_logger, request):
    text = "cat and catbird sit on a catalog"
    rules = [
        RuleSpec("catbird", r"catbird", "BIRD", priority=1),
        RuleSpec("catalog", r"catalog", "LOG", priority=1),
        RuleSpec("cat", r"cat", "FELIX", priority=5),
        RuleSpec("a", r"a", "_A_", priority=0),
    ]
    r = plan(text, rules)
    out = apply(text, r)
    expected = "FELIX _A_nd FELIXbird sit on _A_ FELIX_A_log"
    assert out == expected
    # catbird/catalog appear nowhere; cat appears 3 times
    assert "BIRD" not in out and "LOG" not in out
    accepted_ids = [e.rule_id for e in r.plan.edits]
    assert accepted_ids.count("cat") == 3
    rejects = [d for d in r.decisions if d.stage == "reject"]
    assert any(d.rule_id in ("catbird", "catalog") and d.conflicts_with == "cat"
               for d in rejects)
    run_logger.check(
        request.node.nodeid,
        "priority overlap resolution",
        expected=expected,
        actual=out,
        passed=out == expected,
        reason="cat(prio5) claims its ranges first; lower rules overlapping are "
               "rejected; disjoint 'a' matches still apply",
        intermediate={
            "edits": [(e.start, e.end, e.rule_id) for e in r.plan.edits],
            "rejects": [(d.rule_id, d.start, d.conflicts_with) for d in rejects],
        },
    )


def test_same_priority_resolved_by_rule_id_and_same_start_conflicts():
    # Two rules match the exact same span; rule_id ascending is deterministic.
    text = "abc"
    rules = [
        RuleSpec("zzz", r"abc", "Z"),
        RuleSpec("aaa", r"abc", "A"),
    ]
    r = plan(text, rules)
    assert [e.rule_id for e in r.plan.edits] == ["aaa"]
    # regardless of supplied order
    r2 = plan(text, list(reversed(rules)))
    assert [e.rule_id for e in r2.plan.edits] == ["aaa"]


def test_replacement_text_is_not_rescanned_same_round():
    # 'b' -> 'bb' must NOT cause runaway re-matching; exactly one pass.
    r = plan("ab", [RuleSpec("b", r"b", "bb")])
    assert apply("ab", r) == "abb"
    # a pattern whose replacement text itself matches a rule pattern
    r2 = plan("xx", [RuleSpec("x", r"x", "yx")])
    assert apply("xx", r2) == "yxyx"
    assert r2.plan.edit_count == 2


def test_zero_width_and_non_empty_at_same_start_winner_takes_all():
    # \b at start of a word and a word rule both start at offset 0.
    text = "hello"
    r = plan(text, [
        RuleSpec("bar", r"\b", "|", priority=10),
        RuleSpec("word", r"hello", "HI", priority=1),
    ])
    # bar (higher priority) owns offset 0; word is rejected for same start
    owners = [e.rule_id for e in r.plan.edits]
    assert owners[0] == "bar"
    assert "word" not in owners


def test_zero_width_at_end_boundary_of_adjacent_match_is_allowed():
    # word boundary at offset 5 (end of hello) is adjacent, not overlapping.
    text = "hello world"
    r = plan(text, [
        RuleSpec("word", r"hello", "HI", priority=10),
        RuleSpec("bar", r"\b", "|", priority=1),
    ])
    out = apply(text, r)
    # boundaries: 0 (blocked, same start as hello), 5 (allowed, adjacent end),
    # 6 (start of world), 11 (EOF)
    assert out == "HI| |world|"


def test_zero_width_chains_advance_whole_codepoints_multibyte():
    text = "a€b"
    r = plan(text, [RuleSpec("z", r"z*", ".")])
    # one insertion per codepoint boundary: 0,1,4,5
    assert [(e.start, e.end) for e in r.plan.edits] == [(0, 0), (1, 1), (4, 4), (5, 5)]
    assert apply(text, r) == ".a.€.b."


def test_multibyte_byte_offsets_in_edits():
    text = "a€b"
    r = plan(text, [RuleSpec("euro", r"€", "EURO")])
    (e,) = r.plan.edits
    assert (e.start, e.end) == (1, 4)
    assert e.matched == "€".encode()
    assert apply(text, r) == "aEUROb"


def test_capture_missing_aborts_entire_plan_with_specific_code():
    # bare 'Ms' has no name group -> capture_missing under default policy
    rules = [RuleSpec("t", r"(Mr|Ms)(?:\s+([A-Z][a-z]+))?", "[${1}:$2]")]
    with pytest.raises(CaptureMissingError) as exc:
        plan("Mr Smith; Ms", rules)
    assert exc.value.code == "capture_missing"
    assert exc.value.category == "COMPUTATION"


def test_lenient_missing_capture_substitutes_empty():
    rules = [RuleSpec(
        "t", r"(Mr|Ms)(?:\s+([A-Z][a-z]+))?", "[${1}:$2]",
        missing_capture="empty",
    )]
    r = plan("Mr Smith; Ms; x", rules)
    assert apply("Mr Smith; Ms; x", r) == "[Mr:Smith]; [Ms:]; x"


def test_edit_count_budget_is_enforced():
    limits = PlannerLimits(max_edits=2)
    with pytest.raises(MatchBudgetExceededError) as exc:
        plan("aaaaa", [RuleSpec("a", r"a", "X")], limits=limits)
    assert exc.value.code == "match_budget_exceeded"


def test_plan_is_immutable_and_bound_to_digest():
    r = plan("abc", [RuleSpec("a", r"a", "X")])
    from app.textspec import sha256_hex

    assert r.plan.source_sha256 == sha256_hex(b"abc")
    assert r.plan.is_bound_to(sha256_hex(b"abc"))
    assert not r.plan.is_bound_to(sha256_hex(b"abd"))


def test_decisions_record_accept_and_reject_reasons():
    r = plan("cat", [
        RuleSpec("hi", r"cat", "X", priority=2),
        RuleSpec("lo", r"cat", "Y", priority=1),
    ])
    stages = {(d.rule_id, d.stage) for d in r.decisions}
    assert ("hi", "accept") in stages
    assert ("lo", "reject") in stages
    lo = next(d for d in r.decisions if d.rule_id == "lo")
    assert lo.conflicts_with == "hi"
    assert "same start" in lo.reason
