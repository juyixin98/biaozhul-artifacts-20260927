"""Differential analysis engine: compare an old policy with a new one over the
bounded request space and extract concrete witnesses.

Result semantics (see :mod:`osdiff.types`):

* ``expands``           -- some request moves from fully-denied to proven ALLOW
* ``possibly_expands``  -- some request moves from fully-denied to UNKNOWN/ALLOW;
                           accessibility *might* have grown because unknown
                           conditions make the new verdict uncertain
* ``contracts``         -- proven access was removed

Witness selection is deterministic (transition category, then request identity)
and capped per category; counts always reflect the full enumeration even when
the witness list was truncated, and truncation is flagged rather than hidden.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from . import universe as space_mod
from .evidence import (
    canonical_bytes,
    policy_fingerprint,
    request_identity,
    request_to_jsonable,
    sha256_hex,
)
from .kernel import evaluate
from .policy import Policy, PolicyParseError, parse_policy
from .types import (
    POSSIBLE_EXPANSION_CATEGORIES,
    PROVEN_EXPANSION_CATEGORIES,
    TRANSITIONS,
    Category,
    Failure,
    RunResult,
    Verdict,
    Witness,
)

DEFAULT_WITNESS_LIMIT_PER_CATEGORY = 5


class DiffFailure(Exception):
    def __init__(self, code: Failure, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex


def _sign_payload(result: RunResult) -> dict[str, Any]:
    payload = result.summary()
    payload.pop("signature", None)
    return payload


def run_diff(
    old_doc: Any,
    new_doc: Any,
    *,
    space_cap: int = space_mod.DEFAULT_SPACE_CAP,
    witness_limit_per_category: int = DEFAULT_WITNESS_LIMIT_PER_CATEGORY,
    signer: Callable[[dict[str, Any]], str] | None = None,
    auditor: "Callable[..., None] | None" = None,
    run_id: str | None = None,
    old_version_id: str | None = None,
    new_version_id: str | None = None,
) -> RunResult:
    """Run the full analysis. Raises DiffFailure on any refusal class."""
    run_id = run_id or new_run_id()

    def audit(stage: str, **fields: Any) -> None:
        if auditor is not None:
            auditor(run_id=run_id, stage=stage, **fields)

    try:
        old_policy: Policy = parse_policy(old_doc)
        new_policy: Policy = parse_policy(new_doc)
    except PolicyParseError as e:
        audit("parse-refused", reason=str(e), errors=e.errors)
        raise DiffFailure(Failure.PARSE_ERROR, str(e), details={"errors": e.errors}) from e

    old_hash = policy_fingerprint(old_doc)
    new_hash = policy_fingerprint(new_doc)
    old_version_id = old_version_id or "ver_" + old_hash[:12]
    new_version_id = new_version_id or "ver_" + new_hash[:12]

    axes = space_mod.build_axes(old_policy, new_policy)
    total = space_mod.space_size(axes)
    audit("space-built", space_size=total, basis=space_mod.basis_dict(axes))
    if total > space_cap:
        audit("space-refused", attempted=total, cap=space_cap)
        raise DiffFailure(
            Failure.SPACE_LIMIT_EXCEEDED,
            f"bounded request space has {total} requests, cap is {space_cap}; "
            "refusing to truncate silently -- raise the cap or narrow the policies",
            details={"attempted": total, "cap": space_cap, "basis": space_mod.basis_dict(axes)},
        )

    counts: dict[str, int] = {c.value: 0 for c in Category}
    picked: dict[Category, list[Witness]] = {c: [] for c in Category}
    truncated_categories: set[str] = set()

    for req in space_mod.iter_space(axes, cap=space_cap):
        old_decision = evaluate(old_policy, req)
        new_decision = evaluate(new_policy, req)
        category = TRANSITIONS[(old_decision.verdict, new_decision.verdict)]
        counts[category.value] += 1

        bucket = picked[category]
        if category is not Category.UNCHANGED and len(bucket) < witness_limit_per_category:
            bucket.append(Witness(
                request=request_to_jsonable(req),
                old_verdict=old_decision.verdict,
                new_verdict=new_decision.verdict,
                category=category,
                old_trace=old_decision.trace,
                new_trace=new_decision.trace,
            ))
        elif category is not Category.UNCHANGED and len(bucket) == witness_limit_per_category:
            truncated_categories.add(category.value)

    # Deterministic ordering inside each bucket (request identity), then assemble.
    witnesses: list[Witness] = []
    category_order = [
        Category.EXPANSION_PROVEN,
        Category.EXPANSION_POSSIBLE,
        Category.RESOLVED_UNCERTAINTY,
        Category.CONTRACTION,
        Category.REDUCED_POSSIBLE,
        Category.DENY_TIGHTENED,
        Category.DENY_RELAXED,
    ]
    for cat in category_order:
        bucket = picked[cat]
        bucket.sort(key=lambda w: request_identity(w.request))
        witnesses.extend(bucket)

    expands = any(counts[c.value] > 0 for c in PROVEN_EXPANSION_CATEGORIES)
    possibly_expands = any(counts[c.value] > 0 for c in POSSIBLE_EXPANSION_CATEGORIES)
    contracts = counts[Category.CONTRACTION.value] > 0

    result = RunResult(
        run_id=run_id,
        old_version_id=old_version_id,
        new_version_id=new_version_id,
        old_policy_hash=old_hash,
        new_policy_hash=new_hash,
        space_size=total,
        space_basis=space_mod.basis_dict(axes),
        counts=counts,
        witnesses=witnesses,
        witness_limit=witness_limit_per_category,
        witnesses_truncated=bool(truncated_categories),
        expands=expands,
        possibly_expands=possibly_expands,
        contracts=contracts,
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    # Independent re-check of every emitted witness against both real policies.
    for w in result.witnesses:
        ov = evaluate(old_policy, w.request).verdict
        nv = evaluate(new_policy, w.request).verdict
        if ov is not w.old_verdict or nv is not w.new_verdict:
            raise DiffFailure(
                Failure.EVIDENCE_MISMATCH,
                "internal witness verification failed: recorded verdict not reproduced",
                details={"request": w.request, "recorded": [w.old_verdict.value, w.new_verdict.value],
                         "recomputed": [ov.value, nv.value]},
            )
        expected = TRANSITIONS[(ov, nv)]
        if expected is not w.category:
            raise DiffFailure(
                Failure.EVIDENCE_MISMATCH,
                "internal witness verification failed: transition category not reproduced",
                details={"request": w.request, "recorded": w.category.value, "recomputed": expected.value},
            )
    audit("enumeration-complete", counts=counts, witnesses=len(witnesses))

    if signer is not None:
        result.signature = signer(_sign_payload(result))
    audit("diff-signed" if signer else "diff-unsigned",
          expands=expands, possibly_expands=possibly_expands)
    return result


def result_identity(result: RunResult) -> str:
    payload = _sign_payload(result)
    return sha256_hex(canonical_bytes(payload))
