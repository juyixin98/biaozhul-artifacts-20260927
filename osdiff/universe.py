"""Bounded request-space construction for exhaustive differential analysis.

The analysis is only as sound as the space it enumerates, so the space is built
*from both policies' own boundaries* and its construction is recorded in the
``space_basis`` of every run.

Axes:

* principal  -- every identity named in either policy, plus a synthetic
                 identity named by no policy (probes default-deny).
* action     -- trie regions of every action literal/prefix in either policy.
* resource   -- trie regions of every resource literal/prefix in either policy.
* attributes -- one domain per condition key referenced anywhere:
                  string -> trie regions of its equals/like values, plus unknown
                  number -> every bound, each gap midpoint, just outside ends
                  bool   -> true / false / unknown
                  ip     -> each CIDR boundary and the addresses just outside

Every domain includes the UNKNOWN_VALUE sentinel, so unknown-condition behavior
is exercised at every request-shape, never approximated away.

The full space is the Cartesian product.  Because the problem explicitly scopes
a *restricted* request space, the product is capped; exceeding the cap is a
reported failure (SPACE_LIMIT_EXCEEDED), never a silent truncation.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from decimal import Decimal
from ipaddress import IPv4Address, IPv6Address
from typing import Any, Iterator

from .patterns import GlobPattern, enumerate_regions
from .policy import Policy
from .types import UNKNOWN_VALUE

SYNTHETIC_PRINCIPAL = "identity-named-by-no-statement"
SYNTHETIC_ACTION = "action-named-by-no-statement"
SYNTHETIC_RESOURCE = "resource-named-by-no-statement"

DEFAULT_SPACE_CAP = 200_000


class SpaceLimitExceeded(Exception):
    def __init__(self, attempted: int, cap: int):
        super().__init__(f"request space {attempted} exceeds cap {cap}")
        self.attempted = attempted
        self.cap = cap


@dataclass
class Axis:
    name: str
    values: list[Any]
    kind: str  # "principal" | "action" | "resource" | "attr"


def _as_jsonable(v: Any) -> Any:
    if v is UNKNOWN_VALUE:
        return {"__unknown__": True}
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (IPv4Address, IPv6Address)):
        return str(v)
    return v


def _principal_axis(old: Policy, new: Policy) -> Axis:
    named = sorted({p for pol in (old, new) for st in pol.statements for p in st.principals if p != "*"})
    values: list[str] = named
    if SYNTHETIC_PRINCIPAL not in values:
        values.append(SYNTHETIC_PRINCIPAL)
    return Axis("principal", values, "principal")


def _pattern_axis(old: Policy, new: Policy, *, stmt_field: str,
                  axis_name: str, synthetic: str) -> Axis:
    patterns: list[GlobPattern] = []
    for pol in (old, new):
        for st in pol.statements:
            patterns.extend(getattr(st, stmt_field))
    values = enumerate_regions(patterns)
    if not values:
        values = [synthetic]
    elif synthetic not in values:
        values.append(synthetic)
    return Axis(axis_name, values, axis_name)


def _numeric_reps(decimals: set[Decimal]) -> list[Decimal]:
    bounds = sorted(decimals)
    reps: set[Decimal] = set(bounds)
    reps.add(bounds[0] - Decimal(1))
    reps.add(bounds[-1] + Decimal(1))
    for a, b in zip(bounds, bounds[1:]):
        if b > a:
            reps.add((a + b) / 2)
    return sorted(reps)


def _ip_reps(networks: list[Any]) -> list[Any]:
    reps: set[Any] = set()
    v4 = [n for n in networks if n.version == 4]
    v6 = [n for n in networks if n.version == 6]
    for nets, addr_cls in ((v4, IPv4Address), (v6, IPv6Address)):
        for net in nets:
            reps.add(addr_cls(int(net.network_address)))
            reps.add(addr_cls(int(net.broadcast_address)))
            if int(net.network_address) > 0:
                reps.add(addr_cls(int(net.network_address) - 1))
            reps.add(addr_cls(int(net.broadcast_address) + 1))
    return sorted(reps, key=lambda a: (a.version, int(a)))


def _attribute_axes(old: Policy, new: Policy) -> list[Axis]:
    # key -> {"family", "strings": [...], "numbers": set, "nets": [...], "bool_seen"}
    info: dict[str, dict[str, Any]] = {}
    for pol in (old, new):
        for st in pol.statements:
            for c in st.conditions:
                slot = info.setdefault(c.key, {"family": c.family, "strings": [], "numbers": set(), "nets": []})
                if slot["family"] != c.family:
                    # Different typed uses of one key: domains are merged; the
                    # kernel reports UNKNOWN on type mismatch for either policy.
                    slot["family"] = "mixed"
                if c.family == "string":
                    # StringEquals values are plain strings; StringLike values are
                    # GlobPattern. Normalize both to the restricted pattern type.
                    slot["strings"].extend(
                        v if isinstance(v, GlobPattern) else GlobPattern.parse(v, what=f"condition:{c.key}")
                        for v in c.values
                    )
                elif c.family == "numeric":
                    slot["numbers"].update(c.values)
                elif c.family == "ip":
                    slot["nets"].extend(c.values)

    axes: list[Axis] = []
    for key in sorted(info):
        slot = info[key]
        fam = slot["family"]
        if fam == "string":
            values: list[Any] = enumerate_regions(slot["strings"])
            if not values:
                values = [f"value-not-equal-to-any:{key}"]
            values.append(UNKNOWN_VALUE)
        elif fam == "numeric":
            values = list(_numeric_reps(slot["numbers"])) + [UNKNOWN_VALUE]
        elif fam == "bool":
            values = [True, False, UNKNOWN_VALUE]
        elif fam == "ip":
            values = _ip_reps(slot["nets"]) + [UNKNOWN_VALUE]
        else:  # mixed type usage: no concrete value is safely comparable
            values = [UNKNOWN_VALUE]
        axes.append(Axis(f"attr:{key}", values, "attr"))
    return axes


def build_axes(old: Policy, new: Policy) -> list[Axis]:
    axes = [
        _principal_axis(old, new),
        _pattern_axis(old, new, stmt_field="actions",
                      axis_name="action", synthetic=SYNTHETIC_ACTION),
        _pattern_axis(old, new, stmt_field="resources",
                      axis_name="resource", synthetic=SYNTHETIC_RESOURCE),
    ]
    axes.extend(_attribute_axes(old, new))
    return axes


def iter_space(axes: list[Axis], cap: int = DEFAULT_SPACE_CAP) -> Iterator[dict[str, Any]]:
    size = 1
    for ax in axes:
        size *= len(ax.values)
    if size > cap:
        raise SpaceLimitExceeded(size, cap)

    names = [ax.name for ax in axes]
    for combo in itertools.product(*(ax.values for ax in axes)):
        req: dict[str, Any] = {"attributes": {}}
        for name, value in zip(names, combo):
            if name.startswith("attr:"):
                req["attributes"][name[len("attr:"):]] = value
            else:
                req[name] = value
        yield req


def space_size(axes: list[Axis]) -> int:
    n = 1
    for ax in axes:
        n *= len(ax.values)
    return n


def basis_dict(axes: list[Axis]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for ax in axes:
        key = ax.name if ax.kind == "attr" else ax.kind
        out[key] = {
            "kind": ax.kind,
            "size": len(ax.values),
            "representatives": [_as_jsonable(v) for v in ax.values],
        }
    return out
