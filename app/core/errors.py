"""Explicit failure categories.

The verifier never collapses distinct failures into a generic "bad request"
and never reports success for an exception or an unknown state. Every rejected
proof gets exactly one stable machine-readable category plus a human-readable
reason. These strings are part of the public protocol.
"""
from __future__ import annotations

import enum


class FailCategory(str, enum.Enum):
    # The proof document is not structurally usable (bad hex, truncated
    # sibling path, missing required member, wrong JSON types).
    PROOF_MALFORMED = "PROOF_MALFORMED"
    # A declared scalar cannot be encoded under its declared field type
    # (e.g. "maybe" as bool, NaN as decimal, float-typed int, unknown type).
    TYPE_ENCODING_ERROR = "TYPE_ENCODING_ERROR"
    # value/salt/state do not hash to the field commitment the proof claims.
    COMMITMENT_MISMATCH = "COMMITMENT_MISMATCH"
    # Identity binding is inconsistent: field path/position/record index do
    # not match the claimed leaf, or an index is out of the declared range.
    # This is the category that stops a verifier from accepting a field whose
    # identity has been swapped.
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    # The recomputed field/record Merkle authentication path does not match
    # the parent root embedded in the proof.
    MERKLE_PATH_MISMATCH = "MERKLE_PATH_MISMATCH"
    # Everything inside the proof is internally consistent, but the proof's
    # batch root is not the trusted root the verifier asked about.
    ROOT_MISMATCH = "ROOT_MISMATCH"
    # The prover asked to disclose a field the batch never committed (this is
    # a service-side / request error, not a verdict against a proof).
    FIELD_NOT_COMMITTED = "FIELD_NOT_COMMITTED"
    RECORD_NOT_FOUND = "RECORD_NOT_FOUND"
    BATCH_NOT_FOUND = "BATCH_NOT_FOUND"
    # Policy refusal (e.g. unsalted low-entropy field while disallowed).
    POLICY_VIOLATION = "POLICY_VIOLATION"
    # Unexpected server-side fault. Never returned as a successful verdict.
    INTERNAL_ERROR = "INTERNAL_ERROR"


class CoreError(Exception):
    """Base class for expected, classified kernel/service errors."""

    category: FailCategory = FailCategory.INTERNAL_ERROR

    def __init__(self, reason: str, *, category: FailCategory | None = None):
        super().__init__(reason)
        if category is not None:
            self.category = category

    @property
    def reason(self) -> str:
        return str(self.args[0]) if self.args else self.category.value


class ProofMalformed(CoreError):
    category = FailCategory.PROOF_MALFORMED


class TypeEncodingError(CoreError):
    category = FailCategory.TYPE_ENCODING_ERROR


class CommitmentMismatch(CoreError):
    category = FailCategory.COMMITMENT_MISMATCH


class IdentityMismatch(CoreError):
    category = FailCategory.IDENTITY_MISMATCH


class MerklePathMismatch(CoreError):
    category = FailCategory.MERKLE_PATH_MISMATCH


class RootMismatch(CoreError):
    category = FailCategory.ROOT_MISMATCH


class FieldNotCommitted(CoreError):
    category = FailCategory.FIELD_NOT_COMMITTED


class RecordNotFound(CoreError):
    category = FailCategory.RECORD_NOT_FOUND


class BatchNotFound(CoreError):
    category = FailCategory.BATCH_NOT_FOUND


class PolicyViolation(CoreError):
    category = FailCategory.POLICY_VIOLATION
