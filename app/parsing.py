"""Rule and evidence parsing.

Turns untrusted JSON documents into :class:`CachePolicy` / request /
response contracts. Purely structural validation happens here: anything
that is wrong with the document shape is an ``InputError``. Values that
parse structurally but cannot be canonicalised (for example a header
value that is a JSON list) are left for the kernel and surface as
``ComputationError`` — that keeps the "bad document" vs "cannot compute
the answer for this document" distinction explicit.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .errors import InputError
from .models import (
    KNOWN_DIMENSIONS,
    CachePolicy,
    RequestMeta,
    ResponseMeta,
)

_IDENTITY_MODES = frozenset({"auto", "per_identity", "shared"})
_REQUIRED_REQUEST_FIELDS = frozenset({"id", "method", "path"})
_REQUIRED_RESPONSE_FIELDS = frozenset({"request_id", "status"})


def parse_policy(doc: Any) -> CachePolicy:
    """Validate and build a :class:`CachePolicy` from a raw JSON document."""
    if not isinstance(doc, dict):
        raise InputError(
            "policy must be a JSON object", code="policy.not_object"
        )

    name = doc.get("name", "unnamed-policy")
    if not isinstance(name, str) or not name:
        raise InputError("policy.name must be a non-empty string",
                         code="policy.name")

    dims = doc.get("covered_dimensions")
    if not isinstance(dims, list) or not dims:
        raise InputError(
            "policy.covered_dimensions must be a non-empty list",
            code="policy.dimensions_missing",
        )
    if not all(isinstance(d, str) for d in dims):
        raise InputError(
            "covered_dimensions entries must be strings",
            code="policy.dimension_type",
        )
    unknown = [d for d in dims if d not in KNOWN_DIMENSIONS]
    if unknown:
        raise InputError(
            f"unknown covered dimensions: {unknown}",
            code="policy.unknown_dimension",
            detail={"unknown": unknown},
        )
    if "path" not in dims:
        raise InputError(
            "covered_dimensions must include 'path'",
            code="policy.path_required",
        )

    identity_cfg = doc.get("identity", {})
    if not isinstance(identity_cfg, dict):
        raise InputError("policy.identity must be an object",
                         code="policy.identity_type")
    identity_mode = identity_cfg.get("mode", "auto")
    if identity_mode not in _IDENTITY_MODES:
        raise InputError(
            f"identity.mode must be one of {sorted(_IDENTITY_MODES)}",
            code="policy.identity_mode",
            detail={"mode": identity_mode},
        )

    shared = doc.get("shared", True)
    if not isinstance(shared, bool):
        raise InputError("policy.shared must be a boolean",
                         code="policy.shared_type")

    limits = doc.get("limits", {})
    if not isinstance(limits, dict):
        raise InputError("policy.limits must be an object",
                         code="policy.limits_type")
    try:
        max_requests = int(limits.get("max_requests", 1000))
        max_findings = int(limits.get("max_findings", 500))
    except (TypeError, ValueError):
        raise InputError(
            "policy.limits values must be integers",
            code="policy.limits_type",
        )
    if max_requests < 1 or max_findings < 1:
        raise InputError(
            "policy.limits values must be >= 1",
            code="policy.limits_range",
        )

    return CachePolicy(
        name=name,
        covered_dimensions=tuple(dims),
        identity_mode=identity_mode,
        shared=shared,
        max_requests=max_requests,
        max_findings=max_findings,
    )


def _normalise_headers(raw: Any, *, where: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InputError(
            f"{where}.headers must be an object",
            code="evidence.headers_type",
            detail={"where": where},
        )
    return {str(k).lower(): v for k, v in raw.items()}


def _normalise_query(raw: Any) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    if isinstance(raw, dict):
        items: Iterable[tuple[Any, Any]] = raw.items()
    elif isinstance(raw, list):
        items = (tuple(pair) for pair in raw)
    else:
        raise InputError(
            "query must be an object or list of [key, value] pairs",
            code="evidence.query_type",
        )
    normalised = []
    for pair in items:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise InputError(
                "query entries must be [key, value] pairs",
                code="evidence.query_type",
            )
        key, value = pair
        if not isinstance(key, str):
            raise InputError("query keys must be strings",
                             code="evidence.query_key_type")
        if isinstance(value, list):
            for v in value:
                if not isinstance(v, str):
                    raise InputError(
                        "query values must be strings or lists of strings",
                        code="evidence.query_value_type",
                    )
                normalised.append((key, v))
        elif isinstance(value, str):
            normalised.append((key, value))
        else:
            raise InputError(
                "query values must be strings or lists of strings",
                code="evidence.query_value_type",
            )
    return tuple(sorted(normalised))


def parse_evidence(doc: Any) -> tuple[list[RequestMeta], list[ResponseMeta]]:
    """Validate a raw evidence document into requests and responses."""
    if not isinstance(doc, dict):
        raise InputError(
            "evidence must be a JSON object", code="evidence.not_object"
        )

    raw_requests = doc.get("requests")
    if not isinstance(raw_requests, list) or not raw_requests:
        raise InputError(
            "evidence.requests must be a non-empty list",
            code="evidence.requests_missing",
        )
    raw_responses = doc.get("responses")
    if not isinstance(raw_responses, list) or not raw_responses:
        raise InputError(
            "evidence.responses must be a non-empty list",
            code="evidence.responses_missing",
        )

    requests: list[RequestMeta] = []
    seen_ids: set[str] = set()
    for raw in raw_requests:
        if not isinstance(raw, dict):
            raise InputError(
                "each request must be an object",
                code="evidence.request_type",
            )
        missing = _REQUIRED_REQUEST_FIELDS - raw.keys()
        if missing:
            raise InputError(
                f"request is missing fields: {sorted(missing)}",
                code="evidence.request_fields",
                detail={"missing": sorted(missing)},
            )
        rid = raw["id"]
        if not isinstance(rid, str) or not rid:
            raise InputError("request.id must be a non-empty string",
                             code="evidence.request_id")
        if rid in seen_ids:
            raise InputError(
                f"duplicate request id: {rid!r}",
                code="evidence.duplicate_request",
                detail={"id": rid},
            )
        seen_ids.add(rid)

        if not isinstance(raw["method"], str) or not raw["method"]:
            raise InputError(
                f"request {rid!r} has an invalid method",
                code="evidence.method",
            )
        if not isinstance(raw["path"], str) or not raw["path"].startswith("/"):
            raise InputError(
                f"request {rid!r} path must be an absolute path",
                code="evidence.path",
            )

        requests.append(
            RequestMeta(
                id=rid,
                method=raw["method"].upper(),
                path=raw["path"],
                query=_normalise_query(raw.get("query")),
                headers=_normalise_headers(raw.get("headers"), where=rid),
            )
        )

    responses: list[ResponseMeta] = []
    for raw in raw_responses:
        if not isinstance(raw, dict):
            raise InputError(
                "each response must be an object",
                code="evidence.response_type",
            )
        missing = _REQUIRED_RESPONSE_FIELDS - raw.keys()
        if missing:
            raise InputError(
                f"response is missing fields: {sorted(missing)}",
                code="evidence.response_fields",
                detail={"missing": sorted(missing)},
            )
        request_id = raw["request_id"]
        if request_id not in seen_ids:
            raise InputError(
                f"response references unknown request_id {request_id!r}",
                code="evidence.dangling_response",
                detail={"request_id": request_id},
            )
        status = raw["status"]
        if not isinstance(status, int) or not 100 <= status <= 599:
            raise InputError(
                f"response for {request_id!r} has invalid status {status!r}",
                code="evidence.status",
            )
        body = raw.get("body", "")
        if not isinstance(body, str):
            raise InputError(
                f"response for {request_id!r} body must be a string",
                code="evidence.body",
            )
        responses.append(
            ResponseMeta(
                request_id=request_id,
                status=status,
                headers=_normalise_headers(
                    raw.get("headers"), where=f"response:{request_id}"
                ),
                body=body,
            )
        )

    return requests, responses


def load_fixture(path: str | Path) -> dict[str, Any]:
    """Load a synthetic evidence fixture from a local JSON file."""
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise InputError(
            f"fixture not found: {p}", code="fixture.not_found",
            detail={"path": str(p)},
        )
    except json.JSONDecodeError as exc:
        raise InputError(
            f"fixture {p} is not valid JSON: {exc.msg}",
            code="fixture.invalid_json",
            detail={"path": str(p), "line": exc.lineno, "column": exc.colno},
        )
    except OSError as exc:
        raise InputError(
            f"cannot read fixture {p}: {exc}",
            code="fixture.unreadable",
            detail={"path": str(p)},
        )
