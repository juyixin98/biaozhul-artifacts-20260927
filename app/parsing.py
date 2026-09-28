"""Rule/evidence parsing: bind an incoming share to identity, threshold & field.

This is the policy layer. It decides *why* a share is accepted or rejected
before the security kernel performs any math. Keeping it separate means the
"what counts as a share" rules can be unit-tested without galois or SQLite.

Rules enforced here
-------------------
1. A share is structurally well-formed and has the expected fields.
2. It belongs to the collection named in the request (set identity binding).
3. Its field parameters match the collection's fixed field.
4. Its ``threshold`` / ``total`` binding matches the collection.
5. (kernel) Its independent integrity MAC verifies.
6. A repeated x-coordinate is **never counted twice**. An identical duplicate is
   dropped harmlessly; a duplicate x carrying a *different* value is rejected as
   contradictory evidence rather than silently preferred.
"""
from __future__ import annotations

import enum

from app.core.envelope import ShareEnvelope, fingerprint
from app.core.field import FieldMismatch, FieldParams


class RejectReason(str, enum.Enum):
    MALFORMED = "malformed_share"
    WRONG_COLLECTION = "wrong_collection"
    FIELD_INCOMPATIBLE = "field_parameter_incompatible"
    PARAMETER_MISMATCH = "threshold_total_mismatch"
    BAD_INTEGRITY = "bad_integrity_mac"
    DUPLICATE_X = "duplicate_x_ignored"
    DUPLICATE_X_CONFLICT = "duplicate_x_conflicting_value"
    BLOCK_COUNT_MISMATCH = "block_count_mismatch"


class ShareRejection(Exception):
    """A share could not be admitted to the recovery set.

    ``fp`` is a best-effort fingerprint; it may be ``None`` when the share was
    too malformed to authenticate/identify.
    """

    def __init__(self, reason: RejectReason, detail: str, fp: str | None = None):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.fp = fp


_REQUIRED = ("collection_id", "threshold", "total", "x", "ys", "field", "mac")


def parse_envelope(raw: object) -> ShareEnvelope:
    """Parse raw JSON-like input into a :class:`ShareEnvelope`.

    Raises :class:`ShareRejection` with category :data:`RejectReason.MALFORMED`
    for anything that is not a structurally valid envelope.
    """
    if not isinstance(raw, dict):
        raise ShareRejection(RejectReason.MALFORMED, "share must be an object")
    missing = [k for k in _REQUIRED if k not in raw]
    if missing:
        raise ShareRejection(
            RejectReason.MALFORMED, f"missing fields: {sorted(missing)}"
        )
    try:
        env = ShareEnvelope.from_dict(raw)
    except FieldMismatch as exc:
        # Field incompatibility is a distinct, more specific category.
        raise ShareRejection(
            RejectReason.FIELD_INCOMPATIBLE, str(exc), _safe_fp(raw)
        ) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise ShareRejection(RejectReason.MALFORMED, f"unparseable share: {exc}")

    if not isinstance(env.x, int) or env.x <= 0:
        raise ShareRejection(
            RejectReason.MALFORMED, "x must be a positive integer", _safe_fp(raw)
        )
    if env.threshold <= 0 or env.total <= 0 or env.threshold > env.total:
        raise ShareRejection(
            RejectReason.MALFORMED,
            "require 0 < threshold <= total",
            fingerprint(env),
        )
    if not env.ys or any(not isinstance(y, int) for y in env.ys):
        raise ShareRejection(
            RejectReason.MALFORMED,
            "ys must be a non-empty list of integers",
            fingerprint(env),
        )
    return env


def _safe_fp(raw: dict) -> str | None:
    try:
        return fingerprint(ShareEnvelope.from_dict(raw))
    except Exception:
        return None


def check_bindings(
    env: ShareEnvelope,
    *,
    collection_id: str,
    threshold: int,
    total: int,
    field: FieldParams,
    block_count: int,
) -> None:
    """Reject a parsed share whose identity/params don't match its collection."""
    if env.collection_id != collection_id:
        raise ShareRejection(
            RejectReason.WRONG_COLLECTION,
            f"share belongs to {env.collection_id!r}, request is for "
            f"{collection_id!r}",
            fingerprint(env),
        )
    # parse_envelope already raised FIELD_INCOMPATIBLE for foreign primes, but
    # re-check defensively against the canonical collection parameters.
    if env.field != field:
        raise ShareRejection(
            RejectReason.FIELD_INCOMPATIBLE,
            "share field parameters are incompatible with the collection",
            fingerprint(env),
        )
    if env.threshold != threshold or env.total != total:
        raise ShareRejection(
            RejectReason.PARAMETER_MISMATCH,
            f"share bound to threshold={env.threshold},total={env.total}; "
            f"collection is threshold={threshold},total={total}",
            fingerprint(env),
        )
    if len(env.ys) != block_count:
        raise ShareRejection(
            RejectReason.BLOCK_COUNT_MISMATCH,
            f"share has {len(env.ys)} block(s), collection expects {block_count}",
            fingerprint(env),
        )
