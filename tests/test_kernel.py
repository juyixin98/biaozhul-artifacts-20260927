"""Security kernel tests: concrete verdicts, unknown handling and agreement
with the independent oracle on generated policies/requests."""

from __future__ import annotations

import random

from osdiff.kernel import evaluate
from osdiff.policy import parse_policy
from osdiff.types import UNKNOWN_VALUE, Verdict

from . import fixtures as fx
from .oracle import UNKNOWN as O_UNKNOWN, oracle_decide


def decide(doc, principal, action, resource, **attrs):
    return evaluate(parse_policy(doc), {"principal": principal, "action": action,
                                        "resource": resource, "attributes": attrs})


def test_default_deny_on_empty_policy():
    d = decide(fx.EMPTY, "alice", "s3:GetObject", "photos/x")
    assert d.verdict is Verdict.DENY_NO_MATCH
    assert "default deny" in d.reason


def test_simple_allow_and_scope():
    d = decide(fx.PHOTO_V1, "alice", "s3:GetObject", "photos/cat.png")
    assert d.verdict is Verdict.ALLOW
    outside = decide(fx.PHOTO_V1, "alice", "s3:GetObject", "billing/x")
    assert outside.verdict is Verdict.DENY_NO_MATCH
    other_user = decide(fx.PHOTO_V1, "bob", "s3:GetObject", "photos/cat.png")
    assert other_user.verdict is Verdict.DENY_NO_MATCH


def test_explicit_deny_overrides_allow():
    d = decide(fx.DENY_OVERRIDE_V2, "alice", "s3:GetObject", "photos/secret/x")
    assert d.verdict is Verdict.DENY_EXPLICIT
    allowed = decide(fx.DENY_OVERRIDE_V2, "alice", "s3:GetObject", "photos/public/x")
    assert allowed.verdict is Verdict.ALLOW


def test_unknown_value_under_negated_string_is_not_allowed():
    # StringNotEquals with an unknown attribute => UNKNOWN, never "not equal => allow"
    d = decide(fx.NEG_V2, "alice", "s3:GetObject", "docs/x", department=UNKNOWN_VALUE)
    assert d.verdict is Verdict.UNKNOWN
    assert any(step["outcome"] == "indeterminate" for step in d.trace)
    # known values behave normally
    assert decide(fx.NEG_V2, "alice", "s3:GetObject", "docs/x", department="eng").verdict is Verdict.ALLOW
    assert decide(fx.NEG_V2, "alice", "s3:GetObject", "docs/x", department="external").verdict is Verdict.DENY_NO_MATCH


def test_unknown_numeric_under_less_than_is_unknown():
    d = decide(fx.NEG_NUM_V2, "alice", "s3:GetObject", "docs/x", age=UNKNOWN_VALUE)
    assert d.verdict is Verdict.UNKNOWN
    assert decide(fx.NEG_NUM_V2, "alice", "s3:GetObject", "docs/x", age=10).verdict is Verdict.ALLOW
    assert decide(fx.NEG_NUM_V2, "alice", "s3:GetObject", "docs/x", age=18).verdict is Verdict.DENY_NO_MATCH


def test_if_exists_missing_attribute_is_vacuously_true():
    # no mfa key at all, BoolIfExists => match (this is the documented hypothesis)
    d = decide(fx.IF_EXISTS_V2, "alice", "s3:GetObject", "docs/x")
    assert d.verdict is Verdict.ALLOW
    d_unknown = decide(fx.IF_EXISTS_V2, "alice", "s3:GetObject", "docs/x", mfa=UNKNOWN_VALUE)
    assert d_unknown.verdict is Verdict.ALLOW
    assert decide(fx.IF_EXISTS_V2, "alice", "s3:GetObject", "docs/x", mfa=False).verdict is Verdict.DENY_NO_MATCH


def test_ip_boundary_addresses():
    # 10.0.0.0/30 = .0 .1 .2 .3 ; .4 and the preceding address are outside
    for ip in ("10.0.0.0", "10.0.0.1", "10.0.0.2", "10.0.0.3"):
        assert decide(fx.IP_V2, "alice", "s3:GetObject", "docs/x", source_ip=ip).verdict is Verdict.ALLOW
    for ip in ("9.255.255.255", "10.0.0.4", "10.0.1.0"):
        assert decide(fx.IP_V2, "alice", "s3:GetObject", "docs/x", source_ip=ip).verdict is Verdict.DENY_NO_MATCH
    assert decide(fx.IP_V2, "alice", "s3:GetObject", "docs/x",
                  source_ip=UNKNOWN_VALUE).verdict is Verdict.UNKNOWN
    assert decide(fx.IP_V2, "alice", "s3:GetObject", "docs/x",
                  source_ip="not-an-ip").verdict is Verdict.UNKNOWN


def test_numeric_less_than_equals_boundary():
    assert decide(fx.NUM_V2, "alice", "s3:GetObject", "docs/x", age=17).verdict is Verdict.ALLOW
    assert decide(fx.NUM_V2, "alice", "s3:GetObject", "docs/x", age=18).verdict is Verdict.ALLOW
    assert decide(fx.NUM_V2, "alice", "s3:GetObject", "docs/x", age=19).verdict is Verdict.DENY_NO_MATCH


def test_typed_mismatch_is_unknown_not_error():
    d = decide(fx.NUM_V2, "alice", "s3:GetObject", "docs/x", age="eighteen")
    assert d.verdict is Verdict.UNKNOWN


# ---- agreement with independent oracle on random policies ------------------
OPS_STRING = [("StringEquals", ["eng", "sales"]), ("StringNotEquals", ["external"]),
              ("StringLike", ["proj-*"]), ("StringLikeIfExists", ["proj-*"])]
OPS_NUM = [("NumericLessThan", [18]), ("NumericGreaterThanEquals", [21]), ("NumericNotEquals", [0])]
OPS_IP = [("IpAddress", ["10.0.0.0/8"]), ("NotIpAddress", ["192.168.0.0/16"])]
OPS_BOOL = [("Bool", [True])]


def _random_policy(rng):
    stmts = []
    for i in range(rng.randint(0, 3)):
        cond = {}
        if rng.random() < 0.7:
            op, vals = rng.choice(OPS_STRING + OPS_NUM + OPS_IP + OPS_BOOL)
            key = {"StringEquals": "department", "StringNotEquals": "department",
                   "StringLike": "project", "StringLikeIfExists": "project",
                   "NumericLessThan": "age", "NumericGreaterThanEquals": "age", "NumericNotEquals": "age",
                   "IpAddress": "source_ip", "NotIpAddress": "source_ip", "Bool": "mfa"}[op]
            cond[op] = {key: vals}
        stmts.append(fx.stmt(
            f"s{i}", rng.choice(["Allow", "Deny"]),
            rng.choice([["alice"], ["bob"], ["alice", "bob"], ["*"]]),
            rng.choice([["s3:GetObject"], ["s3:*"], ["s3:DeleteObject"]]),
            rng.choice([["docs/*"], ["docs/private/"], ["*"]]),
            cond or None,
        ))
    return fx.policy(*stmts)


_ATTR_VALUES = {
    "department": ["eng", "sales", "external", O_UNKNOWN],
    "project": ["proj-a", "other", O_UNKNOWN],
    "age": [0, 17, 18, 21, 50, O_UNKNOWN],
    "source_ip": ["10.1.2.3", "192.168.1.1", "8.8.8.8", O_UNKNOWN],
    "mfa": [True, False, O_UNKNOWN],
}


def _random_request(rng):
    attrs = {}
    for key, values in _ATTR_VALUES.items():
        if rng.random() < 0.7:
            v = rng.choice(values)
            if v is not O_UNKNOWN:
                attrs[key] = v
            else:
                attrs[key] = UNKNOWN_VALUE
    return {"principal": rng.choice(["alice", "bob", "carol"]),
            "action": rng.choice(["s3:GetObject", "s3:DeleteObject", "s3:PutObject"]),
            "resource": rng.choice(["docs/x", "docs/private/x", "photos/x", "anything"]),
            "attributes": attrs}


def _oracle_request(req):
    return {"principal": req["principal"], "action": req["action"], "resource": req["resource"],
            "attributes": {k: (O_UNKNOWN if v is UNKNOWN_VALUE else v) for k, v in req["attributes"].items()}}


def test_kernel_agrees_with_independent_oracle_on_1200_cases():
    rng = random.Random(20260927)
    mismatches = []
    for _ in range(40):
        doc = _random_policy(rng)
        parsed = parse_policy(doc)
        for _ in range(30):
            req = _random_request(rng)
            got = evaluate(parsed, req).verdict.value
            want = oracle_decide(doc, _oracle_request(req))
            if got != want:
                mismatches.append((doc, req, got, want))
    assert not mismatches, f"oracle disagreements: {mismatches[:3]}"
