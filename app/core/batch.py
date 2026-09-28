"""Batch construction, disclosure generation and proof verification.

The data model used inside the kernel is plain, JSON-able Python so the same
logic can be exercised directly and cross-checked by an independent verifier
that never imports this package.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.core.commitment import field_commitment
from app.core.encoding import (
    FIELD_TYPES,
    STATE_MISSING,
    STATE_NULL,
    STATE_PRESENT,
)
from app.core.errors import (
    CommitmentMismatch,
    CoreError,
    FieldNotCommitted,
    IdentityMismatch,
    ProofMalformed,
    RecordNotFound,
    RootMismatch,
    TypeEncodingError,
)
from app.core.merkle import (
    authentication_path,
    build_levels,
    verify_path,
)
from app.security.hashing import bytes_equal
from app.security.saltpolicy import (
    SaltPolicy,
    is_declared_low_entropy,
    random_salt,
)

# ---------------------------------------------------------------------------
# Internal (prover-side) model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    path: str
    field_type: str
    # cardinality bound submitted by the evidence source, if known
    value_space: int | None = None


@dataclass(frozen=True)
class FieldCommitmentRecord:
    position: int
    path: str
    field_type: str
    state: str
    commitment_hex: str
    salt_hex: str | None  # private; leaves the trust boundary only on disclose


@dataclass(frozen=True)
class RecordCommitment:
    record_index: int
    record_root_hex: str
    fields: tuple[FieldCommitmentRecord, ...]


@dataclass
class BuiltBatch:
    batch_id: str
    digest: str
    batch_root_hex: str
    record_count: int
    field_count: int
    records: list[RecordCommitment]
    advisories: list[dict[str, Any]] = field(default_factory=list)

    def public_view(self) -> dict[str, Any]:
        """Batch data that may be shown to anyone -- no salts, no values."""
        return {
            "batch_id": self.batch_id,
            "digest": self.digest,
            "batch_root_hex": self.batch_root_hex,
            "record_count": self.record_count,
            "field_count": self.field_count,
            "records": [
                {
                    "record_index": r.record_index,
                    "record_root_hex": r.record_root_hex,
                    "fields": [
                        {
                            "position": f.position,
                            "path": f.path,
                            "field_type": f.field_type,
                            "state": f.state,
                            "commitment_hex": f.commitment_hex,
                        }
                        for f in r.fields
                    ],
                }
                for r in self.records
            ],
            "advisories": self.advisories,
        }


def _canonical_field_specs(fields: list[FieldSpec]) -> list[FieldSpec]:
    if not fields:
        raise ProofMalformed("a batch must declare at least one field")
    paths = [f.path for f in fields]
    if any(not isinstance(p, str) or not p for p in paths):
        raise IdentityMismatch("field paths must be non-empty strings")
    if len(set(paths)) != len(paths):
        raise IdentityMismatch(f"duplicate field paths: {paths}")
    for f in fields:
        if f.field_type not in FIELD_TYPES:
            raise TypeEncodingError(
                f"field {f.path!r} has unsupported type {f.field_type!r}"
            )
    return sorted(fields, key=lambda f: f.path)


def build_batch(
    *,
    batch_id: str,
    fields: list[FieldSpec],
    records: list[dict[str, Any]],
    policy: SaltPolicy,
) -> BuiltBatch:
    """Commit a batch of records against one canonical field schema.

    Each record is a mapping ``path -> {"state": ..., "value": ...}``.
    Fields absent from a record mapping are committed as ``missing``.
    Salts are freshly generated for present values and (by default) nulls.
    """
    if not batch_id:
        raise IdentityMismatch("batch_id must be non-empty")
    ordered = _canonical_field_specs(fields)

    built_records: list[RecordCommitment] = []
    record_nodes: list[bytes] = []
    advisories: list[dict[str, Any]] = []
    private_values: dict[tuple[int, str], tuple[Any, str | None]] = {}

    for idx, spec in enumerate(ordered):
        if is_declared_low_entropy(spec.field_type, spec.value_space):
            advisories.append(
                {
                    "severity": "warning",
                    "code": "LOW_ENTROPY_FIELD",
                    "path": spec.path,
                    "field_type": spec.field_type,
                    "message": (
                        "field has a small value space; commitments hide it "
                        "only while the per-field salt stays private and do "
                        "not prevent enumeration if the salt is exposed"
                    ),
                }
            )

    for rec_index, record in enumerate(records):
        field_nodes: list[bytes] = []
        field_records: list[FieldCommitmentRecord] = []
        for position, spec in enumerate(ordered):
            entry = record.get(spec.path)
            if entry is None:
                state, value, salt = STATE_MISSING, None, None
            else:
                state = entry.get("state", STATE_PRESENT)
                value = entry.get("value")
                if state == STATE_PRESENT:
                    salt = random_salt(policy.salt_bytes)
                elif state == STATE_NULL:
                    # Salting nulls keeps null/presence patterns off-chain;
                    # the salt is still never published for an undisclosed null.
                    salt = random_salt(policy.salt_bytes)
                else:
                    salt = None
                if state not in (STATE_PRESENT, STATE_NULL, STATE_MISSING):
                    raise ProofMalformed(
                        f"record {rec_index} field {spec.path!r}: bad state "
                        f"{state!r}"
                    )

            commitment = field_commitment(
                digest_name=policy.digest_name,
                batch_id=batch_id,
                record_index=rec_index,
                position=position,
                path=spec.path,
                field_type=spec.field_type,
                state=state,
                value=value,
                salt=salt,
            )
            field_nodes.append(commitment)
            if state == STATE_PRESENT:
                private_values[(rec_index, spec.path)] = (value, salt.hex())
            field_records.append(
                FieldCommitmentRecord(
                    position=position,
                    path=spec.path,
                    field_type=spec.field_type,
                    state=state,
                    commitment_hex=commitment.hex(),
                    salt_hex=salt.hex() if salt is not None else None,
                )
            )

        record_root, field_levels = build_levels(
            "field", policy.digest_name, field_nodes
        )
        # Stash levels on the record object via a parallel structure used by
        # disclose(); keep the dataclass small by attaching as attribute.
        record_nodes.append(record_root)
        rc = RecordCommitment(
            record_index=rec_index,
            record_root_hex=record_root.hex(),
            fields=tuple(field_records),
        )
        object.__setattr__(rc, "_field_levels", field_levels)
        built_records.append(rc)

    batch_root, record_levels = build_levels(
        "record", policy.digest_name, record_nodes
    )

    batch = BuiltBatch(
        batch_id=batch_id,
        digest=policy.digest_name,
        batch_root_hex=batch_root.hex(),
        record_count=len(records),
        field_count=len(ordered),
        records=built_records,
        advisories=advisories,
    )
    object.__setattr__(batch, "_record_levels", record_levels)
    object.__setattr__(batch, "_field_specs", ordered)
    object.__setattr__(batch, "_private_values", private_values)
    return batch


# ---------------------------------------------------------------------------
# Disclosure (prover side): reveal one field, keep every other salt/value out
# ---------------------------------------------------------------------------


def disclose_field(batch: BuiltBatch, record_index: int, path: str) -> dict[str, Any]:
    """Build the selective-disclosure proof for exactly one field."""
    if not 0 <= record_index < len(batch.records):
        raise RecordNotFound(
            f"record index {record_index} not found in batch {batch.batch_id}"
        )
    record = batch.records[record_index]
    match = [f for f in record.fields if f.path == path]
    if not match:
        raise FieldNotCommitted(
            f"field {path!r} is not part of batch {batch.batch_id} schema"
        )
    fr = match[0]

    field_levels = getattr(record, "_field_levels", None)
    record_levels = getattr(batch, "_record_levels", None)
    if field_levels is None or record_levels is None:
        raise CoreError(
            "batch object lacks in-memory tree levels; rebuild it before "
            "disclosing (secrets live only in the private store)"
        )

    field_siblings = authentication_path(field_levels, fr.position)
    record_siblings = authentication_path(record_levels, record_index)

    proof: dict[str, Any] = {
        "protocol_version": "audit-commit-v1",
        "digest": batch.digest,
        "batch_id": batch.batch_id,
        "batch_root_hex": batch.batch_root_hex,
        "record_count": batch.record_count,
        "field_count": batch.field_count,
        "claim": {
            "record_index": record_index,
            "position": fr.position,
            "path": fr.path,
            "field_type": fr.field_type,
            "state": fr.state,
            "commitment_hex": fr.commitment_hex,
        },
        "field_tree": {
            "leaf_count": batch.field_count,
            "siblings_hex": [
                None if s is None else s.hex() for s in field_siblings
            ],
            "record_root_hex": record.record_root_hex,
        },
        "record_tree": {
            "leaf_count": batch.record_count,
            "siblings_hex": [
                None if s is None else s.hex() for s in record_siblings
            ],
        },
    }
    # The ONLY place a value or salt appears is the single revealed claim.
    if fr.state == STATE_PRESENT:
        # Re-read the raw value from the caller-provided evidence is not
        # available here; service layer injects value/salt. For direct kernel
        # use, read from attached private material.
        private = getattr(batch, "_private_values", {})
        key = (record_index, path)
        if key not in private:
            raise CoreError(
                "private value material is not attached to this batch"
            )
        value, salt_hex = private[key]
        proof["reveal"] = {"value": value, "salt_hex": salt_hex}
    elif fr.state == STATE_NULL:
        proof["reveal"] = {"value": None, "salt_hex": fr.salt_hex}
    else:
        proof["reveal"] = None  # missing: nothing to reveal
    return proof


# ---------------------------------------------------------------------------
# Verification: pure function over the proof document + a trusted root
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    valid: bool
    category: str | None
    reason: str
    checked_steps: tuple[str, ...]
    trusted_root_hex: str
    proof_root_hex: str
    claim: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "category": self.category,
            "reason": self.reason,
            "checked_steps": list(self.checked_steps),
            "trusted_root_hex": self.trusted_root_hex,
            "proof_root_hex": self.proof_root_hex,
            "claim": self.claim,
        }


def _unhex(name: str, value: Any) -> bytes:
    if not isinstance(value, str):
        raise ProofMalformed(f"{name} must be a hex string")
    try:
        return bytes.fromhex(value)
    except ValueError:
        raise ProofMalformed(f"{name} is not valid hexadecimal")


def verify_proof(
    proof: dict[str, Any],
    *,
    trusted_batch_root_hex: str,
    expected_path: str | None = None,
    expected_record_index: int | None = None,
) -> Verdict:
    """Verify a disclosure proof against a trusted batch root.

    The verifier is fully self-contained: it recomputes the field commitment
    from the revealed (value, salt) and the claim identity, then checks both
    Merkle authentication paths and finally compares the embedded batch root
    against the trusted root. ``expected_path`` / ``expected_record_index``
    additionally pin the claim the caller asked for, so a proof for a
    *different* (even valid) field cannot be substituted.
    """
    steps: list[str] = []
    claim_out: dict[str, Any] | None = None
    try:
        trusted_root = _unhex("trusted_batch_root_hex", trusted_batch_root_hex)
        steps.append("trusted_root_decoded")

        if not isinstance(proof, dict):
            raise ProofMalformed("proof must be a JSON object")
        if proof.get("protocol_version") != "audit-commit-v1":
            raise ProofMalformed(
                f"unsupported protocol version {proof.get('protocol_version')!r}"
            )
        digest_name = proof.get("digest")
        if digest_name not in ("sha256", "sha384", "sha512"):
            raise ProofMalformed(f"unsupported digest {digest_name!r}")
        steps.append("protocol_and_digest_accepted")

        proof_root = _unhex("batch_root_hex", proof.get("batch_root_hex"))
        if len(proof_root) != len(trusted_root):
            raise RootMismatch(
                "proof batch root length does not match the trusted root"
            )
        steps.append("proof_root_decoded")

        claim = proof.get("claim")
        ftree = proof.get("field_tree")
        rtree = proof.get("record_tree")
        reveal = proof.get("reveal", "KEY_ABSENT")
        if not isinstance(claim, dict) or not isinstance(ftree, dict) or not isinstance(
            rtree, dict
        ):
            raise ProofMalformed("proof must contain claim/field_tree/record_tree")

        claimed_commitment = _unhex("claim.commitment_hex", claim.get("commitment_hex"))
        record_index = claim.get("record_index")
        position = claim.get("position")
        path = claim.get("path")
        field_type = claim.get("field_type")
        state = claim.get("state")
        if (
            not isinstance(record_index, int)
            or isinstance(record_index, bool)
            or not isinstance(position, int)
            or isinstance(position, bool)
            or not isinstance(path, str)
            or not path
            or field_type not in FIELD_TYPES
            or state not in (STATE_PRESENT, STATE_NULL, STATE_MISSING)
        ):
            raise ProofMalformed("claim has missing or malformed identity fields")
        if record_index < 0 or position < 0:
            raise IdentityMismatch("negative record index or position")
        steps.append("claim_identity_well_formed")

        # Pin the requested identity BEFORE doing any further work.
        if expected_path is not None and path != expected_path:
            raise IdentityMismatch(
                f"proof is for {path!r}, caller required {expected_path!r}"
            )
        if (
            expected_record_index is not None
            and record_index != expected_record_index
        ):
            raise IdentityMismatch(
                f"proof is for record {record_index}, caller required "
                f"{expected_record_index}"
            )
        steps.append("claim_identity_matches_request")

        field_count = ftree.get("leaf_count")
        record_count = rtree.get("leaf_count")
        declared_field_count = proof.get("field_count")
        declared_record_count = proof.get("record_count")
        if (
            field_count != declared_field_count
            or record_count != declared_record_count
            or not isinstance(field_count, int)
            or not isinstance(record_count, int)
            or position >= field_count
            or record_index >= record_count
            or field_count <= 0
            or record_count <= 0
        ):
            raise IdentityMismatch(
                "leaf counts are inconsistent or index out of range"
            )
        steps.append("indices_within_declared_tree_bounds")

        # --- recompute the field commitment from revealed material ---
        value: Any = None
        salt: bytes | None = None
        if state == STATE_PRESENT:
            if not isinstance(reveal, dict):
                raise ProofMalformed("present claim requires a reveal object")
            value = reveal.get("value")
            salt = _unhex("reveal.salt_hex", reveal.get("salt_hex"))
        elif state == STATE_NULL:
            if reveal is None or not isinstance(reveal, dict):
                raise ProofMalformed("null claim requires reveal {value:null,...}")
            if reveal.get("value") is not None:
                raise CommitmentMismatch("null claim revealed a non-null value")
            salt_hex = reveal.get("salt_hex")
            salt = _unhex("reveal.salt_hex", salt_hex) if salt_hex else None
            value = None
        else:  # missing
            if reveal != "KEY_ABSENT" and reveal is not None:
                raise ProofMalformed(
                    "missing claim must carry no reveal object"
                )
            value, salt = None, None
        steps.append("reveal_material_decoded")

        recomputed = field_commitment(
            digest_name=digest_name,
            batch_id=proof["batch_id"],
            record_index=record_index,
            position=position,
            path=path,
            field_type=field_type,
            state=state,
            value=value,
            salt=salt,
        )
        steps.append("field_commitment_recomputed")

        # Compare against the claimed commitment BEFORE walking either tree:
        # a wrong salt or wrong value fails as COMMITMENT_MISMATCH even though
        # it would also fail the Merkle path, and a forged identity that
        # happens to know a neighbour commitment fails here as well.
        if not bytes_equal(recomputed, claimed_commitment):
            raise CommitmentMismatch(
                "recomputed commitment from value+salt+identity does not equal "
                "the commitment embedded in the proof"
            )
        steps.append("commitment_matches_claimed_leaf")

        field_sibs = _decode_siblings(ftree.get("siblings_hex"))
        record_root = _unhex("field_tree.record_root_hex", ftree.get("record_root_hex"))
        verify_path(
            "field",
            digest_name,
            index=position,
            count=field_count,
            leaf_node=claimed_commitment,
            siblings=field_sibs,
            expected_root=record_root,
        )
        steps.append("field_merkle_path_verified")

        record_sibs = _decode_siblings(rtree.get("siblings_hex"))
        verify_path(
            "record",
            digest_name,
            index=record_index,
            count=record_count,
            leaf_node=record_root,
            siblings=record_sibs,
            expected_root=proof_root,
        )
        steps.append("record_merkle_path_verified")

        if not bytes_equal(proof_root, trusted_root):
            raise RootMismatch(
                "proof is internally consistent but its batch root is not the "
                "trusted root supplied by the caller"
            )
        steps.append("batch_root_matches_trusted_root")

        claim_out = {
            "batch_id": proof["batch_id"],
            "record_index": record_index,
            "position": position,
            "path": path,
            "field_type": field_type,
            "state": state,
            "value": value if state == STATE_PRESENT else None,
        }
        return Verdict(
            valid=True,
            category=None,
            reason="accepted: commitment and both Merkle paths verify against "
            "the trusted batch root and the requested field identity",
            checked_steps=tuple(steps),
            trusted_root_hex=trusted_root.hex(),
            proof_root_hex=proof_root.hex(),
            claim=claim_out,
        )
    except CoreError as exc:
        proof_root_hex = (
            proof.get("batch_root_hex")
            if isinstance(proof, dict) and isinstance(proof.get("batch_root_hex"), str)
            else ""
        )
        return Verdict(
            valid=False,
            category=exc.category.value,
            reason=str(exc),
            checked_steps=tuple(steps),
            trusted_root_hex=trusted_batch_root_hex,
            proof_root_hex=proof_root_hex,
            claim=None,
        )
    except Exception as exc:  # never turn the unknown into success
        return Verdict(
            valid=False,
            category="INTERNAL_ERROR",
            reason=f"unexpected error during verification: {exc!r}",
            checked_steps=tuple(steps),
            trusted_root_hex=trusted_batch_root_hex,
            proof_root_hex="",
            claim=None,
        )


def _decode_siblings(raw: Any) -> list[bytes | None]:
    if not isinstance(raw, list):
        raise ProofMalformed("siblings_hex must be a list")
    out: list[bytes | None] = []
    for item in raw:
        if item is None:
            out.append(None)
        else:
            out.append(_unhex("siblings_hex[]", item))
    return out


def canonical_json(obj: Any) -> str:
    """Stable JSON form used by fixtures and the golden-vector generator."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
