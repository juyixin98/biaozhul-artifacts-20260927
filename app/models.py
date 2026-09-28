"""Immutable data contracts shared across modules.

The parser produces these; the kernel consumes them; the store persists
them. Header names are always lower-cased, query strings always sorted,
so that equality means "the same wire semantics", never "the same bytes".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Mapping

# Request dimensions the keyer understands.
#
# "authorization" and "cookie" are *identity* dimensions: covering them
# means the cache key is scoped per credential. The key stores an HMAC of
# the credential only — raw secrets are never written to the key or the
# database.
KNOWN_DIMENSIONS: tuple[str, ...] = (
    "method",
    "host",
    "path",
    "query",
    "accept",
    "accept-language",
    "accept-encoding",
    "authorization",
    "cookie",
)

IDENTITY_DIMENSIONS: frozenset[str] = frozenset({"authorization", "cookie"})

# Negotiated response header -> the request dimension that selects it.
NEGOTIATION_PAIRS: Mapping[str, str] = {
    "content-language": "accept-language",
    "content-encoding": "accept-encoding",
    "content-type": "accept",
}

# Vary header names that map onto known key dimensions.
VARY_HEADER_TO_DIMENSION: Mapping[str, str] = {
    "accept": "accept",
    "accept-language": "accept-language",
    "accept-encoding": "accept-encoding",
    "authorization": "authorization",
    "cookie": "cookie",
    "host": "host",
}

# Cache-Control directives that forbid shared-cache storage.
PRIVATE_DIRECTIVES: frozenset[str] = frozenset({"private", "no-store"})


@dataclass(frozen=True)
class RequestMeta:
    """Canonicalised view of an observed request."""

    id: str
    method: str
    path: str
    query: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    headers: Mapping[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        return self.headers.get(name)


@dataclass(frozen=True)
class ResponseMeta:
    """Canonicalised view of an observed response."""

    request_id: str
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: str = ""

    @property
    def body_hash(self) -> str:
        return sha256(self.body.encode("utf-8")).hexdigest()

    @property
    def vary(self) -> tuple[str, ...] | None:
        """Parsed Vary.

        ``None`` means the Vary header is absent (distinct from an empty
        list). The string ``*`` is preserved as the wildcard tuple and is
        treated as a separate case by the kernel.
        """
        raw = self.headers.get("vary")
        if raw is None:
            return None
        return tuple(p.strip().lower() for p in raw.split(",") if p.strip())

    @property
    def cache_control(self) -> frozenset[str]:
        raw = self.headers.get("cache-control", "")
        return frozenset(
            p.strip().lower().split("=", 1)[0] for p in raw.split(",") if p.strip()
        )


@dataclass(frozen=True)
class CachePolicy:
    """The declared keying policy under audit.

    - ``covered_dimensions``: request dimensions the policy claims the
      cache key accounts for.
    - ``identity_mode``:
        * ``auto``        — anonymous responses share; authenticated
          requests are keyed per credential and a finding is raised if
          the policy did not declare that explicitly.
        * ``per_identity`` — every key is scoped by credential identity.
        * ``shared``      — the policy explicitly claims responses are
          public even when requests carry credentials. The kernel then
          reports collisions and private-directive violations as
          evidence rather than silently separating them.
    - ``shared``: whether the responses are permitted in a *shared* cache.
    """

    name: str
    covered_dimensions: tuple[str, ...]
    identity_mode: str
    shared: bool
    max_requests: int
    max_findings: int


@dataclass(frozen=True)
class Witness:
    """Two requests that share a key but produced different responses."""

    request_a: str
    request_b: str
    key_prefix: str
    differing_dimensions: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "request_a": self.request_a,
            "request_b": self.request_b,
            "key_prefix": self.key_prefix,
            "differing_dimensions": list(self.differing_dimensions),
        }


# Finding kinds. The set is closed and documented in the README.
FINDING_KINDS = (
    "collision",                  # same key, materially different responses
    "missing_vary",               # responses differ but Vary is absent/incomplete
    "vary_wildcard",              # Vary: * offered into a shared cache
    "uncovered_vary_dimension",   # Vary names a dimension the key ignores
    "unmodelled_vary_dimension",  # Vary names a header the auditor cannot model
    "private_in_shared_cache",    # private/no-store response in a shared policy
    "implicit_identity_keying",   # credentials present but not declared (auto mode)
    "identity_shared",            # credentials present, policy claims "shared"
)

SEVERITIES = ("info", "warning", "critical")


@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str
    rationale: str
    witness: Witness | None = None
    subjects: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "rationale": self.rationale,
            "witness": self.witness.to_dict() if self.witness else None,
            "subjects": list(self.subjects),
        }
