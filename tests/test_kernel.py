"""Kernel tests with independently derived reference values.

Reference keys here are recomputed with the Python *standard library*
hmac/hashlib over a hand-written canonical message — not by calling the
kernel. Expected witness ids, finding kinds and counts are hard-coded
from the fixture files, not produced by the system under test.
"""
from __future__ import annotations

import hashlib
import hmac as stdlib_hmac

import pytest

from app.errors import ComputationError, ResourceExhaustedError
from app.kernel import Kernel
from app.models import CachePolicy, RequestMeta, ResponseMeta
from app.parsing import parse_evidence

from conftest import (
    BROKEN_POLICY,
    BROKEN_SHARED_IDENTITY_POLICY,
    FIXED_POLICY,
    FIXTURES_DIR,
)
from app.parsing import load_fixture, parse_policy

RUN_SECRET = b"0123456789abcdef0123456789abcdef"  # fixed: keys must be reproducible


def _policy(doc: dict) -> CachePolicy:
    return parse_policy(doc)


def _evidence(name: str):
    return parse_evidence(load_fixture(FIXTURES_DIR / f"{name}.json"))


def _run(policy_doc, evidence_name):
    policy = _policy(policy_doc)
    reqs, resps = _evidence(evidence_name)
    findings, events, stats = Kernel(RUN_SECRET).audit(policy, reqs, resps)
    return findings, events, stats


# ---------------------------------------------------------------------------
# key derivation
# ---------------------------------------------------------------------------
def test_key_matches_independent_stdlib_derivation():
    policy = _policy(BROKEN_POLICY)
    req = RequestMeta(
        id="r1",
        method="get",
        path="/greeting",
        query=(),
        headers={"accept-language": "zh-CN"},
    )
    key, message, scoped = Kernel(RUN_SECRET).compute_key(policy, req)

    # Canonical message written by hand from the documented format.
    expected_message = "path=/greeting\nquery="
    assert message == expected_message
    expected_key = stdlib_hmac.new(
        RUN_SECRET, expected_message.encode(), hashlib.sha256
    ).hexdigest()
    assert key == expected_key
    assert scoped is False


def test_accept_encoding_order_is_canonicalised():
    policy = _policy({**BROKEN_POLICY, "covered_dimensions": ["path", "accept-encoding"]})
    req_a = RequestMeta(id="a", method="GET", path="/x", headers={"accept-encoding": "gzip, br"})
    req_b = RequestMeta(id="b", method="GET", path="/x", headers={"accept-encoding": "br,gzip"})
    kernel = Kernel(RUN_SECRET)
    key_a, msg_a, _ = kernel.compute_key(policy, req_a)
    key_b, msg_b, _ = kernel.compute_key(policy, req_b)
    assert key_a == key_b
    assert msg_a == "path=/x\naccept-encoding=br,gzip" == msg_b


def test_credentials_never_appear_in_key_or_logs():
    token = "Bearer super-secret-token-value"
    policy = _policy(BROKEN_POLICY)
    req = RequestMeta(id="r1", method="GET", path="/account", headers={"authorization": token})
    key, message, scoped = Kernel(RUN_SECRET).compute_key(policy, req)
    assert "super-secret-token-value" not in message
    assert "Bearer" not in message
    assert scoped is True  # fail-safe per-identity keying
    expected_tag = stdlib_hmac.new(
        RUN_SECRET, b"identity\0auth\0" + token.encode(), hashlib.sha256
    ).hexdigest()
    assert message == f"path=/account\nquery=\nidentity=auth:{expected_tag}"


def test_distinct_credentials_produce_distinct_keys():
    policy = _policy(BROKEN_POLICY)
    kernel = Kernel(RUN_SECRET)
    req_alice = RequestMeta(id="a", method="GET", path="/account",
                            headers={"authorization": "Bearer alice-token"})
    req_bob = RequestMeta(id="b", method="GET", path="/account",
                          headers={"authorization": "Bearer bob-token"})
    req_anon = RequestMeta(id="c", method="GET", path="/account")
    key_a, _, _ = kernel.compute_key(policy, req_alice)
    key_b, _, _ = kernel.compute_key(policy, req_bob)
    key_c, _, _ = kernel.compute_key(policy, req_anon)
    assert key_a != key_b != key_c and key_a != key_c


# ---------------------------------------------------------------------------
# collision witnesses — concrete ids and dimensions asserted
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "fixture,policy_doc,expected_pair,expected_dim,critical",
    [
        ("language", BROKEN_POLICY, ("r-zh", "r-en"), "accept-language", False),
        ("encoding", BROKEN_POLICY, ("r-gzip", "r-br"), "accept-encoding", False),
        ("missing_vary", BROKEN_POLICY, ("r-zh-novary", "r-en-novary"),
         "accept-language", False),
        ("identity", BROKEN_SHARED_IDENTITY_POLICY, ("r-alice", "r-bob"),
         "identity", True),
    ],
)
def test_broken_policy_produces_exact_collision_witnesses(
    fixture, policy_doc, expected_pair, expected_dim, critical
):
    findings, _events, _stats = _run(policy_doc, fixture)
    collisions = [f for f in findings if f.kind == "collision"]
    assert len(collisions) == 1
    witness = collisions[0].witness
    assert {witness.request_a, witness.request_b} == set(expected_pair)
    assert len(witness.key_prefix) == 16
    assert expected_dim in witness.differing_dimensions
    assert collisions[0].severity == "critical" if critical else "warning"


def test_wildcard_vary_never_groups_and_is_not_repairable_by_keying():
    # No key addition can cover Vary: *; the kernel keeps those
    # responses out of shared groups under BOTH policies, and reports
    # the wildcard as the reason.
    for policy_doc in (BROKEN_POLICY, FIXED_POLICY):
        findings, events, stats = _run(policy_doc, "vary_wildcard")
        assert [f for f in findings if f.kind == "collision"] == []
        assert {f.kind for f in findings} == {"vary_wildcard"}
        assert all(
            e["event"] == "wildcard_response_ungrouped"
            for e in events
            if e["event"] == "wildcard_response_ungrouped"
        )
        assert stats["groups"] == 0


def test_auto_mode_fail_safes_identity_without_cross_identity_collision():
    # Under the default auto policy the kernel separates credentials by
    # force, so no collision exists — but the policy gap is reported.
    findings, _events, stats = _run(BROKEN_POLICY, "identity")
    assert [f for f in findings if f.kind == "collision"] == []
    assert [f.kind for f in findings].count("implicit_identity_keying") == 2
    assert "private_in_shared_cache" in {f.kind for f in findings}
    assert stats["groups"] == 2  # alice and bob were keyed apart


def test_shared_identity_mode_emits_critical_collision_and_identity_shared():
    findings, _events, _stats = _run(BROKEN_SHARED_IDENTITY_POLICY, "identity")
    kinds = [f.kind for f in findings]
    assert kinds.count("identity_shared") == 2
    assert "private_in_shared_cache" in kinds
    collision = next(f for f in findings if f.kind == "collision")
    assert collision.severity == "critical"
    assert {collision.witness.request_a, collision.witness.request_b} == {
        "r-alice", "r-bob"
    }


# ---------------------------------------------------------------------------
# Vary: wildcard vs missing vs incomplete are distinct
# ---------------------------------------------------------------------------
def test_wildcard_vary_is_distinct_from_missing_vary():
    findings_wildcard, _, _ = _run(BROKEN_POLICY, "vary_wildcard")
    findings_missing, _, _ = _run(BROKEN_POLICY, "missing_vary")

    wildcard_kinds = {f.kind for f in findings_wildcard}
    missing_kinds = {f.kind for f in findings_missing}

    assert "vary_wildcard" in wildcard_kinds
    assert "vary_wildcard" not in missing_kinds
    assert "missing_vary" in missing_kinds
    assert "missing_vary" not in wildcard_kinds

    rationale = next(f for f in findings_missing if f.kind == "missing_vary").rationale
    assert "no Vary header at all" in rationale


def test_incomplete_vary_distinguished_from_absent_vary():
    reqs = [
        RequestMeta(id="a", method="GET", path="/x", headers={"accept-language": "en"}),
        RequestMeta(id="b", method="GET", path="/x", headers={"accept-language": "de"}),
    ]
    resps = [
        ResponseMeta(request_id="a", status=200,
                     headers={"content-language": "en", "vary": "accept-encoding"},
                     body="english"),
        ResponseMeta(request_id="b", status=200,
                     headers={"content-language": "de", "vary": "accept-encoding"},
                     body="deutsch"),
    ]
    findings, _, _ = Kernel(RUN_SECRET).audit(_policy(BROKEN_POLICY), reqs, resps)
    mv = [f for f in findings if f.kind == "missing_vary"]
    assert mv and all("does not list" in f.rationale for f in mv)
    assert all("no Vary header at all" not in f.rationale for f in mv)


def test_uncovered_vary_dimension_reported():
    findings, _, _ = _run(BROKEN_POLICY, "language")
    uncovered = [f for f in findings if f.kind == "uncovered_vary_dimension"]
    assert len(uncovered) == 2
    assert all("accept-language" in f.rationale for f in uncovered)


# ---------------------------------------------------------------------------
# after fixing the key, collisions disappear
# ---------------------------------------------------------------------------
def test_fixed_policy_eliminates_all_collisions():
    for name in ["language", "encoding", "identity", "vary_wildcard"]:
        findings, _events, _stats = _run(FIXED_POLICY, name)
        assert [f for f in findings if f.kind == "collision"] == [], (
            f"{name}: fixed key must not collide"
        )


def test_fixed_policy_still_flags_missing_vary_evidence():
    # Fixing the key does not silence a genuinely missing Vary header:
    # the auditor reports policy coverage, it does not invent the header.
    findings, events, stats = _run(FIXED_POLICY, "missing_vary")
    assert [f for f in findings if f.kind == "collision"] == []
    assert stats["groups"] == 2  # different language -> different key now
    assert {f.kind for f in findings} == {"missing_vary"}


def test_events_are_replayable_and_contain_rationale():
    findings, events, stats = _run(BROKEN_POLICY, "language")
    event_names = [e["event"] for e in events]
    assert event_names[0] == "run_started"
    assert "request_keyed" in event_names
    assert "group_formed" in event_names
    assert "pair_compared" in event_names
    assert event_names[-1] == "run_finished"

    pair_event = next(e for e in events if e["event"] == "pair_compared")
    assert pair_event["detail"]["materially_different"] is True
    assert "accept-language" in pair_event["detail"]["differing_dimensions"]

    # Same inputs -> same keys again.
    _, events2, _ = _run(BROKEN_POLICY, "language")
    keys1 = [e["detail"]["key_prefix"] for e in events if e["event"] == "request_keyed"]
    keys2 = [e["detail"]["key_prefix"] for e in events2 if e["event"] == "request_keyed"]
    assert keys1 == keys2


# ---------------------------------------------------------------------------
# failure categories
# ---------------------------------------------------------------------------
def test_finding_limit_is_resource_exhaustion():
    policy = _policy({**BROKEN_POLICY, "limits": {"max_requests": 100, "max_findings": 1}})
    with pytest.raises(ResourceExhaustedError) as exc:
        Kernel(RUN_SECRET).audit(policy, *_evidence("identity"))
    assert exc.value.category.value == "resource"
    assert exc.value.code == "limit.findings"


def test_uncanonicalisable_header_is_computation_error():
    policy = _policy({**BROKEN_POLICY, "covered_dimensions": ["path", "accept-language"]})
    req = RequestMeta(id="a", method="GET", path="/x",
                      headers={"accept-language": ["en", "de"]})  # list, not string
    with pytest.raises(ComputationError) as exc:
        Kernel(RUN_SECRET).compute_key(policy, req)
    assert exc.value.category.value == "computation"
    assert exc.value.code == "compute.header_type"
