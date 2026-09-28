"""Security kernel: key derivation, Vary analysis, collision detection.

Pure computation — no I/O, no clock, no randomness. Everything the kernel
needs (policy, requests, responses, run secret) is injected, which makes
every audit fully replayable: same inputs, same findings, same keys.

Key material discipline:

- Keys are HMAC-SHA256 over a canonical message built from the covered
  dimensions. The run secret is per-run and never persisted.
- Credentials (Authorization / Cookie) are never placed in a key or a
  log. They are replaced by an HMAC identity tag.
- Fail-safe default: in ``auto`` mode a request carrying credentials is
  keyed per identity even if the policy forgot to declare it; the gap is
  reported as ``implicit_identity_keying`` instead of silently sharing.

Findings only claim what the evidence shows: the kernel reports whether
the declared policy covers the observed differences. It never claims a
response is "safe to share" in the abstract.
"""
from __future__ import annotations

from itertools import combinations
from typing import Any
from urllib.parse import urlencode

from cryptography.hazmat.primitives import hashes, hmac

from .errors import ComputationError, ResourceExhaustedError
from .models import (
    IDENTITY_DIMENSIONS,
    NEGOTIATION_PAIRS,
    PRIVATE_DIRECTIVES,
    VARY_HEADER_TO_DIMENSION,
    CachePolicy,
    Finding,
    RequestMeta,
    ResponseMeta,
    Witness,
)

_KEY_PREFIX_LEN = 16


def _normalise_token_list(value: str, *, sort: bool) -> str:
    parts = [p.strip().lower() for p in value.split(",") if p.strip()]
    if sort:
        parts.sort()
    return ",".join(parts)


class Kernel:
    """Derives cache keys and audits them against observed responses."""

    def __init__(self, run_secret: bytes):
        if not isinstance(run_secret, bytes) or len(run_secret) < 16:
            raise ComputationError(
                "run secret must be at least 16 bytes",
                code="compute.secret",
            )
        self._secret = run_secret

    # ------------------------------------------------------------------
    # primitives
    # ------------------------------------------------------------------
    def _hmac_hex(self, msg: bytes) -> str:
        h = hmac.HMAC(self._secret, hashes.SHA256())
        h.update(msg)
        return h.finalize().hex()

    def identity_tag(self, request: RequestMeta) -> str | None:
        """HMAC tag for the request's credentials, or None if anonymous."""
        tags: list[str] = []
        auth = request.header("authorization")
        if auth is not None:
            if not isinstance(auth, str):
                raise ComputationError(
                    f"request {request.id!r}: authorization header is not a string",
                    code="compute.header_type",
                    detail={"request": request.id, "header": "authorization"},
                )
            tags.append("auth:" + self._hmac_hex(b"identity\0auth\0" + auth.encode()))
        cookie = request.header("cookie")
        if cookie is not None:
            if not isinstance(cookie, str):
                raise ComputationError(
                    f"request {request.id!r}: cookie header is not a string",
                    code="compute.header_type",
                    detail={"request": request.id, "header": "cookie"},
                )
            pairs = sorted(p.strip() for p in cookie.split(";") if p.strip())
            tags.append(
                "cookie:" + self._hmac_hex(b"identity\0cookie\0" + ";".join(pairs).encode())
            )
        return "|".join(tags) if tags else None

    def extract_dimension(self, request: RequestMeta, dim: str) -> str:
        """Canonical value of one request dimension."""
        if dim == "method":
            return request.method.upper()
        if dim == "host":
            value = request.header("host") or ""
            if not isinstance(value, str):
                raise ComputationError(
                    f"request {request.id!r}: host header is not a string",
                    code="compute.header_type",
                    detail={"request": request.id, "header": "host"},
                )
            return value.lower()
        if dim == "path":
            return request.path
        if dim == "query":
            return urlencode(sorted(request.query))
        if dim in ("accept", "accept-language", "accept-encoding"):
            value = request.header(dim)
            if value is None:
                return ""
            if not isinstance(value, str):
                raise ComputationError(
                    f"request {request.id!r}: {dim} header is not a string",
                    code="compute.header_type",
                    detail={"request": request.id, "header": dim},
                )
            # Accept-Encoding is unordered per RFC 9110; Accept and
            # Accept-Language carry q-value ordering, so keep their order.
            return _normalise_token_list(value, sort=(dim == "accept-encoding"))
        if dim in IDENTITY_DIMENSIONS:
            # Identity dimensions never appear verbatim in a key; they are
            # folded into the identity tag instead.
            return "<identity>"
        raise ComputationError(
            f"unknown dimension {dim!r}",
            code="compute.unknown_dimension",
            detail={"dimension": dim},
        )

    def compute_key(
        self, policy: CachePolicy, request: RequestMeta
    ) -> tuple[str, str, bool]:
        """Return ``(key_hex, canonical_message, identity_scoped)``."""
        parts = [
            f"{dim}={self.extract_dimension(request, dim)}"
            for dim in policy.covered_dimensions
        ]
        identity = self.identity_tag(request)
        scoped = False
        if policy.identity_mode == "per_identity" or (
            policy.identity_mode == "auto" and identity is not None
        ):
            parts.append(f"identity={identity or 'anonymous'}")
            scoped = True
        message = "\n".join(parts)
        return self._hmac_hex(message.encode()), message, scoped

    # ------------------------------------------------------------------
    # audit
    # ------------------------------------------------------------------
    def audit(
        self,
        policy: CachePolicy,
        requests: list[RequestMeta],
        responses: list[ResponseMeta],
    ) -> tuple[list[Finding], list[dict[str, Any]], dict[str, Any]]:
        """Audit evidence against a policy.

        Returns ``(findings, events, stats)``. ``events`` is the replayable
        run log: keyed requests, formed groups, compared pairs, emitted
        findings — each with the rationale that produced it.
        """
        events: list[dict[str, Any]] = []
        findings: list[Finding] = []

        def emit(event: str, **detail: Any) -> None:
            events.append({"seq": len(events), "event": event, "detail": detail})

        def add_finding(finding: Finding) -> None:
            if len(findings) >= policy.max_findings:
                raise ResourceExhaustedError(
                    f"finding limit {policy.max_findings} exceeded",
                    code="limit.findings",
                    detail={"limit": policy.max_findings},
                )
            findings.append(finding)
            emit(
                "finding_emitted",
                kind=finding.kind,
                severity=finding.severity,
                rationale=finding.rationale,
            )

        emit(
            "run_started",
            policy=policy.name,
            identity_mode=policy.identity_mode,
            shared=policy.shared,
            covered_dimensions=list(policy.covered_dimensions),
            requests=len(requests),
            responses=len(responses),
        )

        by_request: dict[str, ResponseMeta] = {r.request_id: r for r in responses}
        declared_identity = (
            policy.identity_mode == "per_identity"
            or bool(IDENTITY_DIMENSIONS & set(policy.covered_dimensions))
        )

        # ---- key every request --------------------------------------
        groups: dict[str, list[tuple[RequestMeta, ResponseMeta]]] = {}
        for req in requests:
            resp = by_request.get(req.id)
            if resp is None:
                raise ComputationError(
                    f"no response recorded for request {req.id!r}",
                    code="compute.unmatched_request",
                    detail={"request": req.id},
                )
            key, _message, scoped = self.compute_key(policy, req)
            identity = self.identity_tag(req)
            emit(
                "request_keyed",
                request=req.id,
                key_prefix=key[:_KEY_PREFIX_LEN],
                identity_scoped=scoped,
                carries_credentials=identity is not None,
            )

            if identity is not None and not declared_identity:
                if policy.identity_mode == "auto":
                    add_finding(
                        Finding(
                            kind="implicit_identity_keying",
                            severity="warning",
                            rationale=(
                                f"request {req.id!r} carries credentials but the "
                                "policy declares no identity dimension; the kernel "
                                "scoped the key per identity as a fail-safe"
                            ),
                            subjects=(req.id,),
                        )
                    )
                elif policy.identity_mode == "shared":
                    add_finding(
                        Finding(
                            kind="identity_shared",
                            severity="critical",
                            rationale=(
                                f"request {req.id!r} carries credentials but the "
                                "policy mode is 'shared': responses would be "
                                "reused across identities"
                            ),
                            subjects=(req.id,),
                        )
                    )

            # Vary: * responses vary on parameters the cache cannot
            # model; RFC 9110 forbids reusing them for another request, so
            # they never join a shared key group — no added dimension can
            # make their key safe. The per-response loop reports them.
            if resp.vary is not None and "*" in resp.vary:
                emit("wildcard_response_ungrouped", request=req.id)
                continue

            groups.setdefault(key, []).append((req, resp))

        for key, members in groups.items():
            emit(
                "group_formed",
                key_prefix=key[:_KEY_PREFIX_LEN],
                size=len(members),
                members=[req.id for req, _ in members],
            )

        # ---- per-response Vary / Cache-Control analysis --------------
        for req in requests:
            resp = by_request[req.id]
            vary = resp.vary
            if vary is not None and "*" in vary:
                if policy.shared and not (resp.cache_control & PRIVATE_DIRECTIVES):
                    add_finding(
                        Finding(
                            kind="vary_wildcard",
                            severity="critical",
                            rationale=(
                                f"response to {req.id!r} carries 'Vary: *' yet the "
                                "policy permits shared caching; a wildcard Vary "
                                "response must never be reused for another request"
                            ),
                            subjects=(req.id,),
                        )
                    )
                continue  # wildcard subsumes per-header analysis
            if vary:
                for header in vary:
                    dim = VARY_HEADER_TO_DIMENSION.get(header)
                    if dim is None:
                        add_finding(
                            Finding(
                                kind="unmodelled_vary_dimension",
                                severity="info",
                                rationale=(
                                    f"response to {req.id!r} varies on {header!r}, "
                                    "which the auditor cannot model; coverage of "
                                    "this dimension cannot be verified"
                                ),
                                subjects=(req.id,),
                            )
                        )
                    elif dim not in policy.covered_dimensions and dim not in IDENTITY_DIMENSIONS:
                        add_finding(
                            Finding(
                                kind="uncovered_vary_dimension",
                                severity="warning",
                                rationale=(
                                    f"response to {req.id!r} declares 'Vary: {header}' "
                                    f"but the policy key does not cover {dim!r}"
                                ),
                                subjects=(req.id,),
                            )
                        )
                    elif dim in IDENTITY_DIMENSIONS and not declared_identity:
                        add_finding(
                            Finding(
                                kind="uncovered_vary_dimension",
                                severity="warning",
                                rationale=(
                                    f"response to {req.id!r} declares 'Vary: {header}' "
                                    "but the policy declares no identity dimension"
                                ),
                                subjects=(req.id,),
                            )
                        )

            if policy.shared and resp.cache_control & PRIVATE_DIRECTIVES:
                directives = sorted(resp.cache_control & PRIVATE_DIRECTIVES)
                add_finding(
                    Finding(
                        kind="private_in_shared_cache",
                        severity="critical",
                        rationale=(
                            f"response to {req.id!r} is marked Cache-Control: "
                            f"{', '.join(directives)} but the policy stores it in a "
                            "shared cache"
                        ),
                        subjects=(req.id,),
                    )
                )

        # ---- collision detection within key groups -------------------
        for key, members in groups.items():
            if len(members) < 2:
                continue
            for (req_a, resp_a), (req_b, resp_b) in combinations(members, 2):
                differing = self._differing_dimensions(req_a, resp_a, req_b, resp_b)
                material = self._materially_different(resp_a, resp_b)
                emit(
                    "pair_compared",
                    request_a=req_a.id,
                    request_b=req_b.id,
                    key_prefix=key[:_KEY_PREFIX_LEN],
                    materially_different=material,
                    differing_dimensions=list(differing),
                )
                if not material:
                    continue
                identity_involved = (
                    self.identity_tag(req_a) is not None
                    or self.identity_tag(req_b) is not None
                )
                add_finding(
                    Finding(
                        kind="collision",
                        severity="critical" if identity_involved else "warning",
                        rationale=(
                            f"requests {req_a.id!r} and {req_b.id!r} share cache key "
                            f"{key[:_KEY_PREFIX_LEN]}… but produced materially "
                            f"different responses; the key does not cover "
                            f"{sorted(differing) or ['origin variance']}"
                        ),
                        witness=Witness(
                            request_a=req_a.id,
                            request_b=req_b.id,
                            key_prefix=key[:_KEY_PREFIX_LEN],
                            differing_dimensions=tuple(sorted(differing)),
                        ),
                        subjects=(req_a.id, req_b.id),
                    )
                )

        # ---- missing-Vary scan, independent of key groups -------------
        # A missing/incomplete Vary header is an origin defect even when a
        # correct key happens to separate the requests: the response set
        # itself fails to declare what it varies on. Scan all responses to
        # the same method+path resource and dedupe per (subject, dimension).
        reported: set[tuple[str, str]] = set()
        for (req_a, resp_a), (req_b, resp_b) in combinations(
            [(r, by_request[r.id]) for r in requests], 2
        ):
            if req_a.method != req_b.method or req_a.path != req_b.path:
                continue
            for response_header, dim in NEGOTIATION_PAIRS.items():
                if resp_a.headers.get(response_header) == resp_b.headers.get(response_header):
                    continue
                if (req_a.header(dim) or "") == (req_b.header(dim) or ""):
                    continue
                for req, resp in ((req_a, resp_a), (req_b, resp_b)):
                    if (req.id, dim) in reported:
                        continue
                    reported.add((req.id, dim))
                    vary = resp.vary
                    if vary is None:
                        add_finding(
                            Finding(
                                kind="missing_vary",
                                severity="warning",
                                rationale=(
                                    f"responses for {req_a.id!r}/{req_b.id!r} differ on "
                                    f"{response_header} but the response to {req.id!r} "
                                    "has no Vary header at all"
                                ),
                                subjects=(req.id,),
                            )
                        )
                    elif "*" not in vary and dim not in vary:
                        add_finding(
                            Finding(
                                kind="missing_vary",
                                severity="warning",
                                rationale=(
                                    f"responses for {req_a.id!r}/{req_b.id!r} differ on "
                                    f"{response_header} but the response to {req.id!r} "
                                    f"has Vary: {', '.join(vary)} which does not list it"
                                ),
                                subjects=(req.id,),
                            )
                        )

        stats = {
            "groups": len(groups),
            "largest_group": max((len(m) for m in groups.values()), default=0),
            "key_prefixes": sorted(k[:_KEY_PREFIX_LEN] for k in groups),
        }
        emit(
            "run_finished",
            findings=len(findings),
            collisions=sum(1 for f in findings if f.kind == "collision"),
            groups=stats["groups"],
        )
        return findings, events, stats

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _materially_different(a: ResponseMeta, b: ResponseMeta) -> bool:
        if a.body_hash != b.body_hash:
            return True
        for header in NEGOTIATION_PAIRS:
            if a.headers.get(header) != b.headers.get(header):
                return True
        return False

    def _differing_dimensions(
        self,
        req_a: RequestMeta,
        resp_a: ResponseMeta,
        req_b: RequestMeta,
        resp_b: ResponseMeta,
    ) -> set[str]:
        """Dimensions on which the two requests differ, plus any negotiated
        response difference mapped back to its selecting dimension."""
        dims: set[str] = set()
        for header, dim in NEGOTIATION_PAIRS.items():
            if resp_a.headers.get(header) != resp_b.headers.get(header):
                dims.add(dim)
        for dim in ("accept", "accept-language", "accept-encoding"):
            if (req_a.header(dim) or "") != (req_b.header(dim) or ""):
                dims.add(dim)
        if self.identity_tag(req_a) != self.identity_tag(req_b):
            dims.add("identity")
        if req_a.query != req_b.query:
            dims.add("query")
        if req_a.method != req_b.method:
            dims.add("method")
        return dims
