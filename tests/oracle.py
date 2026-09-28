"""Independent reference oracle for the test suite.

This module deliberately imports NOTHING from the osdiff package.  It is a
second implementation of the same policy semantics written in a different
style (set-based evaluation, no dataclasses, explicit Kleene helpers), so that
agreement between oracle and kernel is evidence of correctness rather than the
kernel confirming itself.

Verdicts are plain strings: "ALLOW", "DENY_EXPLICIT", "DENY_NO_MATCH",
"UNKNOWN". Unknown attribute values are the marker string "<<UNKNOWN_VALUE>>".
"""

from __future__ import annotations

import ipaddress as _ip
import json
from decimal import Decimal as _D

UNKNOWN = "<<UNKNOWN_VALUE>>"


# ---------- independent pattern semantics -----------------------------------
def _pat_match(pattern: str, text: str) -> bool:
    # Restricted language, re-derived independently: a trailing '*' prefix,
    # otherwise exact equality. Reject anything else (same refusal contract).
    if pattern.count("*") == 0:
        return text == pattern
    if pattern.count("*") == 1 and pattern.endswith("*"):
        return text.startswith(pattern[:-1])
    raise ValueError(f"oracle rejects non-restricted pattern: {pattern!r}")


def _any_pat(patterns, text):
    return any(_pat_match(p, text) for p in patterns)


# ---------- three-valued logic ----------------------------------------------
# Values: True / False / None  (None == UNKNOWN)
def _k_and(a, b):
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def _k_or(xs):
    if any(x is True for x in xs):
        return True
    if any(x is None for x in xs):
        return None
    return False


def _k_not(x):
    return None if x is None else (not x)


# ---------- clause evaluation -----------------------------------------------
def _numeric_hit(op, actual, targets):
    ts = [_D(str(t)) for t in targets]
    a = _D(str(actual))
    if op == "NumericEquals":
        return any(a == t for t in ts)
    if op == "NumericNotEquals":
        return all(a != t for t in ts)
    if op == "NumericLessThan":
        return any(a < t for t in ts)
    if op == "NumericLessThanEquals":
        return any(a <= t for t in ts)
    if op == "NumericGreaterThan":
        return any(a > t for t in ts)
    if op == "NumericGreaterThanEquals":
        return any(a >= t for t in ts)
    raise AssertionError(op)


def _clause(op, key, targets, if_exists, attrs):
    if key not in attrs or attrs[key] == UNKNOWN:
        if if_exists:
            return True  # vacuous under the "...IfExists" hypothesis
        return None
    val = attrs[key]

    if op in ("StringEquals", "StringNotEquals"):
        hit = val in targets
        return hit if op == "StringEquals" else (not hit)
    if op in ("StringLike", "StringNotLike"):
        hit = any(_pat_match(t, val) for t in targets)
        return hit if op == "StringLike" else (not hit)
    if op.startswith("Numeric"):
        if isinstance(val, bool):
            return None
        try:
            hit = _numeric_hit(op, val, targets)
        except Exception:
            return None
        return hit
    if op == "Bool":
        return val in targets if isinstance(val, bool) else None
    if op in ("IpAddress", "NotIpAddress"):
        try:
            addr = _ip.ip_address(val)
        except (ValueError, TypeError):
            return None
        inside = any(addr in _ip.ip_network(t, strict=False) for t in targets)
        return inside if op == "IpAddress" else (not inside)
    raise AssertionError(op)


def _split_op(raw_op):
    table = {
        "StringEquals", "StringNotEquals", "StringLike", "StringNotLike",
        "NumericEquals", "NumericNotEquals", "NumericLessThan",
        "NumericLessThanEquals", "NumericGreaterThan", "NumericGreaterThanEquals",
        "Bool", "IpAddress", "NotIpAddress",
    }
    if raw_op in table:
        return raw_op, False
    if raw_op.endswith("IfExists") and raw_op[: -len("IfExists")] in table:
        return raw_op[: -len("IfExists")], True
    raise ValueError(f"oracle: unknown operator {raw_op}")


def oracle_decide(policy: dict, request: dict) -> str:
    """Independent verdict computation. `policy` is the raw JSON document."""
    attrs = request.get("attributes", {}) or {}
    allow_fired = False
    deny_fired = False
    allow_indet = False
    deny_indet = False

    for st in policy["Statement"]:
        principals = st["Principal"]
        principals = [principals] if isinstance(principals, str) else principals
        scope_principal = "*" in principals or request["principal"] in principals
        scope_action = _any_pat(_list(st["Action"]), request["action"])
        scope_resource = _any_pat(_list(st["Resource"]), request["resource"])
        if not (scope_principal and scope_action and scope_resource):
            continue

        result = True
        for raw_op, clauses in (st.get("Condition") or {}).items():
            op, if_exists = _split_op(raw_op)
            for key, targets in clauses.items():
                # Values for one key are OR'd (Kleene); clauses are AND'd below.
                hits = [_clause(op, key, [t], if_exists, attrs) for t in _list(targets)]
                result = _k_and(result, _k_or(hits))

        if result is True:
            if st["Effect"] == "Deny":
                deny_fired = True
            else:
                allow_fired = True
        elif result is None:
            if st["Effect"] == "Deny":
                deny_indet = True
            else:
                allow_indet = True

    if deny_fired:
        return "DENY_EXPLICIT"
    if allow_fired:
        return "ALLOW"
    if deny_indet or allow_indet:
        return "UNKNOWN"
    return "DENY_NO_MATCH"


def _list(v):
    if isinstance(v, list):
        return v
    return [v]


def oracle_decide_text(policy_text, request):
    return oracle_decide(json.loads(policy_text), request)
