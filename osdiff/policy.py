"""Policy / rule parsing and validation.

A policy document has the shape::

    {
      "Version": "2026-09-01",
      "Statement": [
        {
          "Sid": "label",
          "Effect": "Allow" | "Deny",
          "Principal": ["alice"],            # opaque identities; "*" means any
          "Action":    ["s3:GetObject"],     # literal or trailing-* prefix
          "Resource":  ["photos/*"],         # literal or trailing-* prefix
          "Condition": {
            "StringEquals": {"department": ["eng"]},
            "NumericLessThanEquals": {"age_years": 65},
            "IpAddress": {"source_ip": ["10.0.0.0/8"]},
            "Bool": {"mfa_present": true},
            "StringLike": {"project": ["proj-a-*"]}
          }
        }
      ]
    }

Principals are opaque strings -- there is deliberately no user/role backend.

The parser's job is *refusal*: anything the rest of the engine cannot reason
about exhaustively is rejected here with a precise location instead of being
guessed at later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from ipaddress import IPv4Network, IPv6Network, ip_network
from typing import Any

from .patterns import GlobPattern, PatternError

POLICY_VERSION = "2026-09-01"

_STRING_OPS = {"StringEquals", "StringNotEquals", "StringLike", "StringNotLike"}
_NUMERIC_OPS = {
    "NumericEquals", "NumericNotEquals",
    "NumericLessThan", "NumericLessThanEquals",
    "NumericGreaterThan", "NumericGreaterThanEquals",
}
_BOOL_OPS = {"Bool"}
_IP_OPS = {"IpAddress", "NotIpAddress"}
KNOWN_OPS = _STRING_OPS | _NUMERIC_OPS | _BOOL_OPS | _IP_OPS


class PolicyParseError(ValueError):
    def __init__(self, message: str, errors: list[dict[str, str]] | None = None):
        super().__init__(message)
        self.errors = errors or []


@dataclass(frozen=True)
class Condition:
    op: str                 # base op without IfExists suffix
    key: str
    raw_values: tuple[Any, ...]
    values: tuple[Any, ...]  # parsed: str | GlobPattern | Decimal | bool | IPvxNetwork
    if_exists: bool
    location: str

    @property
    def negated(self) -> bool:
        return self.op in ("StringNotEquals", "StringNotLike", "NumericNotEquals", "NotIpAddress")

    @property
    def family(self) -> str:
        if self.op in _STRING_OPS:
            return "string"
        if self.op in _NUMERIC_OPS:
            return "numeric"
        if self.op in _BOOL_OPS:
            return "bool"
        return "ip"


@dataclass(frozen=True)
class Statement:
    sid: str
    effect: str                      # "Allow" | "Deny"
    principals: tuple[str, ...]
    actions: tuple[GlobPattern, ...]
    resources: tuple[GlobPattern, ...]
    conditions: tuple[Condition, ...]


@dataclass(frozen=True)
class Policy:
    version: str
    statements: tuple[Statement, ...]
    raw: dict[str, Any] = field(hash=False)


def _split_op(op: str) -> tuple[str, bool]:
    if op in KNOWN_OPS:
        return op, False
    if op.endswith("IfExists"):
        base = op[: -len("IfExists")]
        if base in KNOWN_OPS:
            return base, True
    raise PolicyParseError(f"unknown condition operator {op!r}")


def _parse_values(op: str, raw: Any, location: str) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    if not isinstance(raw, list) or not raw:
        raise PolicyParseError(
            f"{location}: condition values must be a non-empty array",
            [{"location": location, "problem": "values-not-array-or-empty"}],
        )
    raws: list[Any] = []
    parsed: list[Any] = []
    for i, item in enumerate(raw):
        loc = f"{location}.values[{i}]"
        if op in ("StringEquals", "StringNotEquals"):
            if not isinstance(item, str):
                raise PolicyParseError(f"{loc}: expected string")
            raws.append(item)
            parsed.append(item)
        elif op in ("StringLike", "StringNotLike"):
            p = GlobPattern.parse(item, what=loc)
            raws.append(p.raw)
            parsed.append(p)
        elif op in _NUMERIC_OPS:
            if isinstance(item, bool) or not isinstance(item, (int, float, str)):
                raise PolicyParseError(f"{loc}: expected JSON number (boolean is not a number)")
            try:
                dec = Decimal(str(item))
            except InvalidOperation:
                raise PolicyParseError(f"{loc}: unparseable number {item!r}")
            if not dec.is_finite():
                raise PolicyParseError(f"{loc}: number must be finite")
            raws.append(item)
            parsed.append(dec)
        elif op == "Bool":
            if not isinstance(item, bool):
                raise PolicyParseError(f"{loc}: expected JSON boolean")
            raws.append(item)
            parsed.append(item)
        elif op in ("IpAddress", "NotIpAddress"):
            if not isinstance(item, str):
                raise PolicyParseError(f"{loc}: expected CIDR string")
            try:
                net = ip_network(item, strict=False)
            except ValueError:
                raise PolicyParseError(f"{loc}: invalid CIDR {item!r}")
            if not isinstance(net, (IPv4Network, IPv6Network)):  # pragma: no cover
                raise PolicyParseError(f"{loc}: unsupported IP version")
            raws.append(item)
            parsed.append(net)
    return tuple(raws), tuple(parsed)


def _parse_condition_block(block: Any, st_loc: str) -> tuple[Condition, ...]:
    if not isinstance(block, dict):
        raise PolicyParseError(f"{st_loc}.Condition: must be an object")
    out: list[Condition] = []
    idx = 0
    for op_raw, clauses in block.items():
        base_op, if_exists = _split_op(op_raw)
        if not isinstance(clauses, dict):
            raise PolicyParseError(f"{st_loc}.Condition.{op_raw}: must be an object of key -> values")
        for key, raw_values in clauses.items():
            if not isinstance(key, str) or not key:
                raise PolicyParseError(f"{st_loc}.Condition.{op_raw}: condition key must be a non-empty string")
            location = f"{st_loc}.Condition[{idx}]({op_raw}:{key})"
            raw_tuple, parsed_tuple = _parse_values(base_op, raw_values, location)
            out.append(Condition(base_op, key, raw_tuple, parsed_tuple, if_exists, location))
            idx += 1
    return tuple(out)


def _parse_statement(st: Any, index: int) -> Statement:
    loc = f"Statement[{index}]"
    if not isinstance(st, dict):
        raise PolicyParseError(f"{loc}: must be an object")

    effect = st.get("Effect")
    if effect not in ("Allow", "Deny"):
        raise PolicyParseError(
            f"{loc}.Effect: must be 'Allow' or 'Deny' (no implicit effect)",
            [{"location": f"{loc}.Effect", "problem": f"bad-effect:{effect!r}"}],
        )

    def names(field: str) -> tuple[str, ...]:
        v = st.get(field)
        if v is None:
            raise PolicyParseError(f"{loc}.{field}: required")
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
            raise PolicyParseError(f"{loc}.{field}: must be a non-empty string or array of non-empty strings")
        return tuple(v)

    def globs(field: str) -> tuple[GlobPattern, ...]:
        v = st.get(field)
        if v is None:
            raise PolicyParseError(f"{loc}.{field}: required")
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list) or not v:
            raise PolicyParseError(f"{loc}.{field}: must be a non-empty string or array")
        parsed_patterns: list[GlobPattern] = []
        for j, x in enumerate(v):
            try:
                parsed_patterns.append(GlobPattern.parse(x, what=f"{loc}.{field}[{j}]"))
            except PatternError as e:
                raise PolicyParseError(
                    str(e),
                    [{"location": f"{loc}.{field}[{j}]", "problem": "unsupported-pattern-syntax"}],
                ) from e
        return tuple(parsed_patterns)

    sid = st.get("Sid", f"stmt{index}")
    if not isinstance(sid, str) or not sid:
        raise PolicyParseError(f"{loc}.Sid: must be a non-empty string")

    principals = names("Principal")
    actions = globs("Action")
    resources = globs("Resource")
    conditions = _parse_condition_block(st["Condition"], loc) if "Condition" in st else ()
    return Statement(sid, effect, principals, actions, resources, conditions)


def parse_policy(doc: Any) -> Policy:
    """Parse and validate a policy document. Raises PolicyParseError on refusal."""
    if not isinstance(doc, dict):
        raise PolicyParseError("policy must be a JSON object")
    version = doc.get("Version", doc.get("version"))
    if version != POLICY_VERSION:
        raise PolicyParseError(
            f"unsupported Version {version!r}; expected {POLICY_VERSION!r}",
            [{"location": "Version", "problem": f"bad-version:{version!r}"}],
        )
    stmts_raw = doc.get("Statement", doc.get("statement"))
    if stmts_raw is None:
        raise PolicyParseError("policy must contain a Statement array")
    if isinstance(stmts_raw, dict):
        stmts_raw = [stmts_raw]
    if not isinstance(stmts_raw, list):
        raise PolicyParseError("Statement must be an array")
    statements = tuple(_parse_statement(st, i) for i, st in enumerate(stmts_raw))
    return Policy(version=POLICY_VERSION, statements=statements, raw=doc)


def parse_policy_text(text: str | bytes) -> Policy:
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        raise PolicyParseError(f"policy is not valid JSON: {e}") from e
    return parse_policy(doc)
