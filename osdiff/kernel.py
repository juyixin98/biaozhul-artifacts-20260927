"""Three-valued security kernel.

Ordering: explicit Deny beats Allow (explicit-refusal-first), Allow beats
default-deny, and any condition referencing an unknown value produces
UNKNOWN rather than being optimistically treated as a match.

Condition clause semantics (one key with a list of values = OR over values):

* known value present: normal comparison; ``Not*`` operators negate normally.
* value absent / unknown and operator has IfExists: clause is vacuously True
  (the attribute's absence is explicitly part of the policy's hypothesis).
* value absent / unknown, ordinary operator: UNKNOWN -- including for ``Not*``
  operators.  Negating an unknown still leaves it unknown; we never let
  "we don't know" silently turn into "allowed" (or into a denial either:
  downstream statements still get to decide).

Within a statement, Principal/Action/Resource are ordinary booleans and all
Condition clauses combine with AND (Kleene).  A statement that does not fire
at all is absent from the trace; an indeterminate statement is recorded with
``outcome: "indeterminate"`` so the final UNKNOWN verdict is explainable.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .policy import Condition, Policy, Statement
from .types import UNKNOWN_VALUE, Verdict


class _Unknown:
    pass


_U = _Unknown()
_Tri = bool | _Unknown


def _not(x: _Tri) -> _Tri:
    if x is _U:
        return _U
    return not x


def _and(a: _Tri, b: _Tri) -> _Tri:
    if a is False or b is False:
        return False
    if a is _U or b is _U:
        return _U
    return True


def _or(values: list[_Tri]) -> _Tri:
    if any(v is True for v in values):
        return True
    if any(v is _U for v in values):
        return _U
    return False


def _compare_numeric(op: str, attr: Decimal, values: tuple[Any, ...]) -> bool:
    if op == "NumericEquals":
        return any(attr == v for v in values)
    if op == "NumericNotEquals":
        return all(attr != v for v in values)
    if op == "NumericLessThan":
        return any(attr < v for v in values)
    if op == "NumericLessThanEquals":
        return any(attr <= v for v in values)
    if op == "NumericGreaterThan":
        return any(attr > v for v in values)
    if op == "NumericGreaterThanEquals":
        return any(attr >= v for v in values)
    raise AssertionError(f"unhandled numeric op {op}")  # pragma: no cover


def _eval_condition(cond: Condition, request: dict[str, Any]) -> tuple[_Tri, str]:
    present = cond.key in request["attributes"]
    raw = request["attributes"].get(cond.key, UNKNOWN_VALUE)
    if raw is UNKNOWN_VALUE:
        if cond.if_exists:
            return True, f"{cond.key}: absent/unknown and {cond.op}IfExists -> vacuous-true"
        return _U, f"{cond.key}: unknown value -> UNKNOWN"

    if cond.family == "string":
        if not isinstance(raw, str):
            return _U, f"{cond.key}: typed {type(raw).__name__}, expected string -> UNKNOWN"
        if cond.op in ("StringEquals", "StringNotEquals"):
            eq = any(raw == v for v in cond.values)
            return (eq if cond.op == "StringEquals" else not eq), (
                f"{cond.key}={raw!r} {'in' if cond.op == 'StringEquals' else 'not in'} {list(cond.raw_values)}"
            )
        # StringLike / StringNotLike against trailing-* globs
        hits = [v.matches(raw) for v in cond.values]
        like = any(hits)
        return (like if cond.op == "StringLike" else not like), (
            f"{cond.key}={raw!r} {'like' if cond.op == 'StringLike' else 'not-like'} {list(cond.raw_values)}"
        )

    if cond.family == "numeric":
        if isinstance(raw, bool) or not isinstance(raw, (int, float, Decimal, str)):
            return _U, f"{cond.key}: typed {type(raw).__name__}, expected numeric -> UNKNOWN"
        try:
            dec = Decimal(str(raw))
        except InvalidOperation:
            return _U, f"{cond.key}: unparseable numeric {raw!r} -> UNKNOWN"
        if not dec.is_finite():
            return _U, f"{cond.key}: non-finite numeric {raw!r} -> UNKNOWN"
        return _compare_numeric(cond.op, dec, cond.values), (
            f"{cond.key}={dec} {cond.op[len('Numeric'):]} {[str(v) for v in cond.values]}"
        )

    if cond.family == "bool":
        if not isinstance(raw, bool):
            return _U, f"{cond.key}: typed {type(raw).__name__}, expected boolean -> UNKNOWN"
        return any(raw is v for v in cond.values), f"{cond.key}={raw} Bool {list(cond.values)}"

    # ip
    import ipaddress

    try:
        addr = ipaddress.ip_address(raw)
    except (ValueError, TypeError):
        return _U, f"{cond.key}: {raw!r} is not a parseable IP address -> UNKNOWN"
    if addr.version not in (4, 6):  # pragma: no cover
        return _U, f"{cond.key}: unsupported IP version -> UNKNOWN"
    inside = any(addr in net for net in cond.values)
    return (inside if cond.op == "IpAddress" else not inside), (
        f"{cond.key}={raw} {'in' if cond.op == 'IpAddress' else 'not-in'} {list(cond.raw_values)}"
    )


def _glob_any(patterns: tuple[Any, ...], value: str) -> bool:
    return any(p.matches(value) for p in patterns)


def evaluate_statement(st: Statement, request: dict[str, Any]) -> tuple[_Tri, dict[str, Any]]:
    """Return whether the statement fires (True / False / UNKNOWN) plus a trace step."""
    principal_ok = "*" in st.principals or request["principal"] in st.principals
    action_ok = _glob_any(st.actions, request["action"])
    resource_ok = _glob_any(st.resources, request["resource"])

    base = principal_ok and action_ok and resource_ok
    scope_reason = (
        f"principal={'match' if principal_ok else 'no-match'} "
        f"action={'match' if action_ok else 'no-match'} "
        f"resource={'match' if resource_ok else 'no-match'}"
    )
    if not base:
        return False, {
            "sid": st.sid,
            "effect": st.effect,
            "outcome": "not-in-scope",
            "detail": scope_reason,
        }

    combined: _Tri = True
    clause_details: list[str] = []
    for cond in st.conditions:
        tri, detail = _eval_condition(cond, request)
        clause_details.append(detail)
        combined = _and(combined, tri)
        if combined is False:
            break

    if combined is True:
        return True, {
            "sid": st.sid,
            "effect": st.effect,
            "outcome": "matched",
            "detail": scope_reason + " | " + " | ".join(clause_details),
        }
    if combined is False:
        return False, {
            "sid": st.sid,
            "effect": st.effect,
            "outcome": "condition-false",
            "detail": scope_reason + " | " + " | ".join(clause_details),
        }
    return _U, {
        "sid": st.sid,
        "effect": st.effect,
        "outcome": "indeterminate",
        "detail": scope_reason + " | " + " | ".join(clause_details),
    }


@dataclass
class Decision:
    verdict: Verdict
    reason: str
    trace: list[dict[str, Any]]


def evaluate(policy: Policy, request: dict[str, Any]) -> Decision:
    """Evaluate one request against one policy. Never raises on UNKNOWN input:
    unknown condition values yield Verdict.UNKNOWN with an explanatory trace.
    """
    trace: list[dict[str, Any]] = []
    fired_allow = False
    fired_deny = False
    indeterminate_allow = False
    indeterminate_deny = False

    for st in policy.statements:
        tri, step = evaluate_statement(st, request)
        if tri is True:
            trace.append(step)
            if st.effect == "Deny":
                fired_deny = True
            else:
                fired_allow = True
        elif tri is _U:
            trace.append(step)
            if st.effect == "Deny":
                indeterminate_deny = True
            else:
                indeterminate_allow = True
        else:
            trace.append(step)

    # Explicit refuse first, even over an Allow.
    if fired_deny:
        return Decision(Verdict.DENY_EXPLICIT, "explicit Deny statement matched (deny overrides allow)", trace)
    if fired_allow:
        return Decision(Verdict.ALLOW, "Allow statement matched, no Deny matched", trace)

    if indeterminate_deny or indeterminate_allow:
        which = []
        if indeterminate_deny:
            which.append("Deny")
        if indeterminate_allow:
            which.append("Allow")
        return Decision(
            Verdict.UNKNOWN,
            f"indeterminate {','.join(which)} statement(s): condition(s) reference unknown value; "
            "neither allow nor deny proven (default is NOT to allow)",
            trace,
        )
    return Decision(Verdict.DENY_NO_MATCH, "default deny: no statement granted access", trace)
