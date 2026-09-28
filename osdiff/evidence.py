"""Evidence: request identity, canonical serialization and witness re-checking.

A witness is only trustworthy if its recorded verdict can be reproduced by
re-evaluating the exact recorded request under the exact recorded policy.  The
kernel's verdict and the recorded verdict are therefore compared independently
of the diff enumeration; disagreement is an EVIDENCE_MISMATCH failure rather
than a quietly inconsistent result.
"""

from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal
from typing import Any

from .kernel import Decision, evaluate
from .policy import Policy
from .types import UNKNOWN_JSON_TAG, UNKNOWN_VALUE, Verdict


def _json_default(obj: Any) -> Any:
    if obj is UNKNOWN_VALUE:
        return copy.deepcopy(UNKNOWN_JSON_TAG)
    if isinstance(obj, Decimal):
        return str(obj)
    if hasattr(obj, "compressed"):  # ipaddress IPv4/6 networks/addresses
        return str(obj)
    raise TypeError(f"cannot canonically serialize {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default)


def canonical_bytes(obj: Any) -> bytes:
    return canonical_json(obj).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def policy_fingerprint(doc: dict[str, Any]) -> str:
    """Stable identity for a policy document (independent of dict key order)."""
    return sha256_hex(canonical_bytes(doc))


def normalize_request(raw: Any) -> dict[str, Any]:
    """Validate and normalize a request coming from JSON/external evidence."""
    if not isinstance(raw, dict):
        raise ValueError("request must be an object")
    principal = raw.get("principal")
    action = raw.get("action")
    resource = raw.get("resource")
    for name, val in (("principal", principal), ("action", action), ("resource", resource)):
        if not isinstance(val, str) or not val:
            raise ValueError(f"request.{name} must be a non-empty string")
    attrs_raw = raw.get("attributes", {})
    if not isinstance(attrs_raw, dict):
        raise ValueError("request.attributes must be an object")

    attrs: dict[str, Any] = {}
    for key, val in attrs_raw.items():
        if not isinstance(key, str) or not key:
            raise ValueError("attribute keys must be non-empty strings")
        if isinstance(val, dict) and val == UNKNOWN_JSON_TAG:
            attrs[key] = UNKNOWN_VALUE
        else:
            attrs[key] = val
    return {"principal": principal, "action": action, "resource": resource, "attributes": attrs}


def request_identity(request: dict[str, Any]) -> str:
    return sha256_hex(canonical_bytes(request))


def request_to_jsonable(request: dict[str, Any]) -> dict[str, Any]:
    return json.loads(canonical_json(request))


def recheck(policy: Policy, request: dict[str, Any]) -> Decision:
    return evaluate(policy, request)


def verify_verdict(policy: Policy, request: dict[str, Any], expected: Verdict) -> tuple[bool, Verdict]:
    actual = recheck(policy, request).verdict
    return actual is expected, actual
