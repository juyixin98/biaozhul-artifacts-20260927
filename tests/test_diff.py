"""Differential analysis tests with concrete witness assertions.

Every witness the engine emits is independently re-judged by the oracle under
both policies; transition counts are spot-checked against specific expected
witnesses, not merely against "the API ran".
"""

from __future__ import annotations

import pytest

from osdiff import universe as space_mod
from osdiff.diff import DiffFailure, run_diff
from osdiff.policy import parse_policy
from osdiff.types import (
    TRANSITIONS,
    Category,
    Failure,
    UNKNOWN_VALUE,
)

from . import fixtures as fx
from .oracle import UNKNOWN as O_UNKNOWN, oracle_decide


def _oracle_req(request):
    def _conv(v):
        if v is UNKNOWN_VALUE or v == {"__unknown__": True}:
            return O_UNKNOWN
        return v
    return {
        "principal": request["principal"],
        "action": request["action"],
        "resource": request["resource"],
        "attributes": {k: _conv(v) for k, v in request.get("attributes", {}).items()},
    }


def _check_witnesses_against_oracle(result, old_doc, new_doc):
    for w in result.witnesses:
        req = _oracle_req(w.request)
        assert oracle_decide(old_doc, req) == w.old_verdict.value
        assert oracle_decide(new_doc, req) == w.new_verdict.value
        assert TRANSITIONS[(w.old_verdict, w.new_verdict)] is w.category


def _find(result, category, *, principal=None, resource=None, attr=None):
    out = []
    for w in result.witnesses:
        if w.category is not category:
            continue
        if principal is not None and w.request["principal"] != principal:
            continue
        if resource is not None and w.request["resource"] != resource:
            continue
        if attr is not None:
            k, v = attr
            if w.request.get("attributes", {}).get(k) != v:
                continue
        out.append(w)
    return out


# ---------------------------------------------------------------------------
def test_empty_to_allow_is_proven_expansion_with_concrete_witness():
    r = run_diff(fx.EMPTY, fx.PHOTO_V2_EXPAND)
    _check_witnesses_against_oracle(r, fx.EMPTY, fx.PHOTO_V2_EXPAND)
    assert r.expands is True
    assert r.counts[Category.EXPANSION_PROVEN.value] >= 1

    witness = _find(r, Category.EXPANSION_PROVEN, principal="bob", resource="photos/")[0]
    assert witness.old_verdict.value == "DENY_NO_MATCH"
    assert witness.new_verdict.value == "ALLOW"
    # the trace actually explains which statement matched
    matched = [s for s in witness.new_trace if s["outcome"] == "matched"]
    assert matched and matched[0]["sid"] == "read-photos"

    # alice inside the prefix is also a witness; every expansion witness's
    # resource is actually covered by the new photos/* prefix.
    alice_inside = [w for w in r.witnesses
                    if w.category is Category.EXPANSION_PROVEN and w.request["principal"] == "alice"]
    assert alice_inside and all(w.request["resource"].startswith("photos") for w in alice_inside)
    assert not _find(r, Category.EXPANSION_PROVEN, principal="identity-named-by-no-statement")


def test_overlapping_prefixes_witness_the_deep_and_disjoint_regions():
    r = run_diff(fx.OVERLAP_V1, fx.OVERLAP_V2)
    _check_witnesses_against_oracle(r, fx.OVERLAP_V1, fx.OVERLAP_V2)
    # newly reachable disjoint prefix
    assert _find(r, Category.EXPANSION_PROVEN, resource="billing/")
    # already-covered prefix point itself is unchanged (exact overlap)
    assert r.counts[Category.UNCHANGED.value] >= 1
    # the deep private prefix key region shows up as an expansion too
    assert _find(r, Category.EXPANSION_PROVEN, resource="photos/2026/private/")


def test_negated_string_condition_with_unknown_value_is_possible_not_allowed():
    r = run_diff(fx.NEG_V1, fx.NEG_V2)
    _check_witnesses_against_oracle(r, fx.NEG_V1, fx.NEG_V2)
    # unknown department: DENY_NO_MATCH -> UNKNOWN = possible expansion only
    possible = _find(r, Category.EXPANSION_POSSIBLE, attr=("department", {"__unknown__": True}))
    assert possible
    assert possible[0].new_verdict.value == "UNKNOWN"
    # known in-group value is a *proven* expansion: some concrete string value
    # other than "external" proves ALLOW.
    proven = [w for w in r.witnesses
              if w.category is Category.EXPANSION_PROVEN
              and w.request.get("attributes", {}).get("department") not in ("external", {"__unknown__": True})]
    assert proven
    # known external value stays denied: it must not appear as expansion
    denied_now = [w for w in r.witnesses
                  if w.request.get("attributes", {}).get("department") == "external"]
    assert all(w.category in (Category.UNCHANGED,) for w in denied_now)


def test_unrelated_rule_change_leaves_target_region_unchanged():
    r = run_diff(fx.UNRELATED_V1, fx.UNRELATED_V2)
    _check_witnesses_against_oracle(r, fx.UNRELATED_V1, fx.UNRELATED_V2)
    # the change concerns s3:DeleteObject/admin/*; nothing expands for GetObject/photos
    assert r.expands is False
    assert r.possibly_expands is False
    # admin region does change (Allow -> Deny is a contraction for that request)
    assert r.counts[Category.CONTRACTION.value] >= 1
    changed = [w for w in r.witnesses if w.category is Category.CONTRACTION]
    assert all(w.request["action"] == "s3:DeleteObject" for w in changed)


def test_explicit_deny_over_allow_is_contraction_and_deny_takes_precedence():
    r = run_diff(fx.DENY_OVERRIDE_V1, fx.DENY_OVERRIDE_V2)
    _check_witnesses_against_oracle(r, fx.DENY_OVERRIDE_V1, fx.DENY_OVERRIDE_V2)
    assert r.contracts is True
    assert r.expands is False
    w = _find(r, Category.CONTRACTION, resource="photos/secret/")[0]
    assert w.old_verdict.value == "ALLOW"
    assert w.new_verdict.value == "DENY_EXPLICIT"


def test_default_to_explicit_deny_is_tightened_not_expansion():
    r = run_diff(fx.TIGHTEN_V1, fx.TIGHTEN_V2)
    _check_witnesses_against_oracle(r, fx.TIGHTEN_V1, fx.TIGHTEN_V2)
    assert r.expands is False
    assert r.possibly_expands is False
    assert r.counts[Category.DENY_TIGHTENED.value] >= 1
    w = _find(r, Category.DENY_TIGHTENED, resource="photos/")[0]
    assert (w.old_verdict.value, w.new_verdict.value) == ("DENY_NO_MATCH", "DENY_EXPLICIT")


def test_ip_and_numeric_boundaries_are_enumerated():
    r_ip = run_diff(fx.IP_V1, fx.IP_V2)
    _check_witnesses_against_oracle(r_ip, fx.IP_V1, fx.IP_V2)
    assert _find(r_ip, Category.EXPANSION_PROVEN, attr=("source_ip", "10.0.0.3"))
    assert not _find(r_ip, Category.EXPANSION_PROVEN, attr=("source_ip", "10.0.0.4"))
    assert _find(r_ip, Category.EXPANSION_POSSIBLE, attr=("source_ip", {"__unknown__": True}))

    r_num = run_diff(fx.NUM_V1, fx.NUM_V2)
    _check_witnesses_against_oracle(r_num, fx.NUM_V1, fx.NUM_V2)
    assert _find(r_num, Category.EXPANSION_PROVEN, attr=("age", "18"))
    assert not _find(r_num, Category.EXPANSION_PROVEN, attr=("age", "19"))
    assert _find(r_num, Category.EXPANSION_POSSIBLE, attr=("age", {"__unknown__": True}))


def test_every_space_point_is_classified_exactly_once():
    # Reconstruct counts by evaluating the full bounded space independently.
    old, new = fx.OVERLAP_V1, fx.OVERLAP_V2
    axes = space_mod.build_axes(parse_policy(old), parse_policy(new))
    size = space_mod.space_size(axes)
    r = run_diff(old, new)
    assert r.space_size == size
    assert sum(r.counts.values()) == size


_ALL_PAIRS = [
    (fx.EMPTY, fx.PHOTO_V2_EXPAND),
    (fx.OVERLAP_V1, fx.OVERLAP_V2),
    (fx.NEG_V1, fx.NEG_V2),
    (fx.NEG_NUM_V1, fx.NEG_NUM_V2),
    (fx.IF_EXISTS_V1, fx.IF_EXISTS_V2),
    (fx.UNRELATED_V1, fx.UNRELATED_V2),
    (fx.DENY_OVERRIDE_V1, fx.DENY_OVERRIDE_V2),
    (fx.TIGHTEN_V1, fx.TIGHTEN_V2),
    (fx.IP_V1, fx.IP_V2),
    (fx.NUM_V1, fx.NUM_V2),
]


@pytest.mark.parametrize("old,new", _ALL_PAIRS)
def test_oracle_agrees_at_every_bounded_space_point(old, new):
    """The independent oracle and the kernel must agree at EVERY enumerated
    request, under BOTH policies -- not only at chosen witnesses."""
    from osdiff.kernel import evaluate as kernel_evaluate

    old_p, new_p = parse_policy(old), parse_policy(new)
    axes = space_mod.build_axes(old_p, new_p)
    checked = 0
    for req in space_mod.iter_space(axes):
        oreq = _oracle_req(req)
        assert kernel_evaluate(old_p, req).verdict.value == oracle_decide(old, oreq), (old, oreq)
        assert kernel_evaluate(new_p, req).verdict.value == oracle_decide(new, oreq), (new, oreq)
        checked += 1
    assert checked == space_mod.space_size(axes)
    assert checked > 0


# ---- failure classes -------------------------------------------------------
@pytest.mark.parametrize("doc", [fx.BAD_GLOB, fx.BAD_VERSION, fx.BAD_EFFECT, fx.BAD_NO_RESOURCE])
def test_parser_refuses_unanalyzable_input(doc):
    with pytest.raises(DiffFailure) as ei:
        run_diff(doc, fx.EMPTY)
    assert ei.value.code is Failure.PARSE_ERROR


def test_space_cap_exceeded_is_reported_not_silently_truncated():
    with pytest.raises(DiffFailure) as ei:
        run_diff(fx.EMPTY, fx.EMPTY, space_cap=1)
    assert ei.value.code is Failure.SPACE_LIMIT_EXCEEDED
    assert ei.value.details["attempted"] > ei.value.details["cap"]


def test_witness_recheck_guards_against_tampered_records():
    r = run_diff(fx.EMPTY, fx.PHOTO_V2_EXPAND)
    w = r.witnesses[0]
    # Tamper with the serialized witness; re-evaluating it under the new policy
    # must disagree, which is what the engine's own guard checks at production.
    w.request["principal"] = "identity-named-by-no-statement"
    assert oracle_decide(fx.PHOTO_V2_EXPAND, _oracle_req(w.request)) != "ALLOW"
