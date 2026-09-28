"""重叠裁决：优先级、包含、确定性顺序。"""
from __future__ import annotations

from app.core.redactor import Candidate, resolve_overlaps


def _c(rule_id, s, e, *, priority=50, source="pattern", repl="<X>"):
    return Candidate(rule_id=rule_id, start=s, end=e, replacement=repl,
                     priority=priority, source=source,
                     matched_text="m")


def test_lower_priority_number_wins():
    a = _c("high", 0, 10, priority=10)
    b = _c("low", 2, 8, priority=90)
    accepted, rejected = resolve_overlaps([a, b])
    assert [c.rule_id for c in accepted] == ["high"]
    assert rejected[0].loser.rule_id == "low"
    assert rejected[0].reason == "OVERLAP_PRIORITY"


def test_containment_same_priority_longer_wins():
    big = _c("big", 0, 10, priority=20)
    small = _c("small", 2, 6, priority=20)
    accepted, rejected = resolve_overlaps([big, small])
    assert [c.rule_id for c in accepted] == ["big"]
    assert rejected[0].reason == "OVERLAP_CONTAINED"


def test_adjacent_spans_not_overlapping():
    a = _c("a", 0, 10)
    b = _c("b", 10, 20)
    accepted, rejected = resolve_overlaps([a, b])
    assert {c.rule_id for c in accepted} == {"a", "b"}
    assert rejected == []


def test_deterministic_order_on_full_tie():
    # 完全同区间同优先级：按 rule_id 字典序，结果确定
    a = _c("rule-aaa", 0, 5, priority=20)
    b = _c("rule-zzz", 0, 5, priority=20)
    accepted, rejected = resolve_overlaps([b, a])
    assert [c.rule_id for c in accepted] == ["rule-aaa"]
    assert rejected[0].loser.rule_id == "rule-zzz"


def test_field_source_breaks_tie_before_pattern():
    f = _c("field-x", 0, 8, priority=20, source="field")
    p = _c("pattern-x", 0, 8, priority=20, source="pattern")
    accepted, _ = resolve_overlaps([p, f])
    assert accepted[0].source == "field"


def test_partial_overlap_earliest_start_wins_same_priority():
    # [0,10) 与 [5,15) 同优先级：起点早者胜
    a = _c("a", 0, 10, priority=20)
    b = _c("b", 5, 15, priority=20)
    accepted, rejected = resolve_overlaps([a, b])
    assert [c.rule_id for c in accepted] == ["a"]
    assert rejected[0].loser.rule_id == "b"
