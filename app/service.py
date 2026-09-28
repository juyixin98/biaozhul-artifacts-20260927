"""Application service: orchestrates parsing, kernel, storage and audit.

The API layer stays thin; all use cases live here so they can be tested
without HTTP. Verification itself does NOT touch this service -- it is a pure
kernel function anyone can run against a trusted root, which is exactly the
property selective disclosure requires.
"""
from __future__ import annotations

import json
from typing import Any

from app import SERVICE_VERSION
from app.audit.logging_config import RunBoundLogger
from app.core import merkle as merkle_mod
from app.core.batch import (
    build_batch,
    verify_proof,
)
from app.core.errors import CoreError
from app.core.merkle import authentication_path
from app.parsing import parse_field_specs, parse_records
from app.security.redaction import fingerprint
from app.security.saltpolicy import SaltPolicy
from app.storage import Database


class CommitmentService:
    def __init__(self, db: Database, policy: SaltPolicy, log: RunBoundLogger):
        self.db = db
        self.policy = policy
        self.log = log

    # ------------------------------------------------------------------ create
    def create_batch(
        self,
        *,
        batch_id: str,
        fields_raw: list[dict[str, Any]],
        records_raw: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.log.step(
            "create_batch:begin",
            extra={"batch_id": batch_id, "n_fields": len(fields_raw)},
        )
        specs = parse_field_specs(fields_raw)
        records = parse_records(records_raw, specs)
        self.log.step(
            "create_batch:parsed",
            extra={"n_records": len(records), "n_fields": len(specs)},
        )

        batch = build_batch(
            batch_id=batch_id, fields=specs, records=records, policy=self.policy
        )
        self.log.step(
            "create_batch:committed",
            extra={
                "batch_root": batch.batch_root_hex,
                "advisories": len(batch.advisories),
            },
        )

        secrets: list[dict[str, Any]] = []
        for rec in batch.records:
            for fr in rec.fields:
                cell = records[rec.record_index][fr.path]
                entry = {
                    "record_index": rec.record_index,
                    "path": fr.path,
                    "state": fr.state,
                    "salt_hex": fr.salt_hex,
                }
                if fr.state == "present":
                    entry["value"] = cell["value"]
                secrets.append(entry)

        public_json = json.dumps(batch.public_view(), ensure_ascii=False, sort_keys=True)
        self.db.insert_batch(
            batch_id=batch.batch_id,
            digest=batch.digest,
            batch_root=batch.batch_root_hex,
            field_count=batch.field_count,
            record_count=batch.record_count,
            public_json=public_json,
            secrets=secrets,
            run_id=self.log.run_id,
        )
        self.db.insert_audit_event(
            {
                "run_id": self.log.run_id,
                "event_type": "BATCH_COMMITTED",
                "batch_id": batch.batch_id,
                "verdict": "OK",
                "category": None,
                "fingerprint": fingerprint(
                    (batch_id, len(specs), len(records)), self.policy.digest_name
                ),
                "detail": {
                    "service_version": SERVICE_VERSION,
                    "digest": batch.digest,
                    "batch_root": batch.batch_root_hex,
                    "record_count": batch.record_count,
                    "field_count": batch.field_count,
                    "advisory_codes": [a["code"] for a in batch.advisories],
                    # No values, no salts anywhere in this payload.
                },
            }
        )
        self.log.step("create_batch:stored", extra={"batch_id": batch.batch_id})
        return batch.public_view()

    # ---------------------------------------------------------------- disclose
    def disclose(
        self, *, batch_id: str, record_index: int, path: str
    ) -> dict[str, Any]:
        self.log.step(
            "disclose:begin",
            extra={"batch_id": batch_id, "record_index": record_index, "path": path},
        )
        public = self.db.public_batch(batch_id)
        root = self.db.batch_root(batch_id)
        if root != public["batch_root_hex"]:  # pragma: no cover - storage invariant
            raise CoreError("stored root inconsistency")
        record = self._find_public_record(public, record_index)
        field = self._find_public_field(record, path)

        # Rebuild the two trees from the STORED public commitments (no values
        # needed) to obtain authentication paths. Commitments are the trust
        # anchor; rebuilding proves the paths match the committed structure.
        field_commitments: list[bytes] = [
            bytes.fromhex(f["commitment_hex"]) for f in record["fields"]
        ]
        record_nodes = [
            bytes.fromhex(r["record_root_hex"]) for r in public["records"]
        ]
        field_root_check, field_levels = merkle_mod.build_levels(
            "field", public["digest"], field_commitments
        )
        if field_root_check.hex() != record["record_root_hex"]:  # pragma: no cover
            raise CoreError("stored field tree inconsistency")
        _, record_levels = merkle_mod.build_levels(
            "record", public["digest"], record_nodes
        )

        field_sibs = authentication_path(field_levels, field["position"])
        record_sibs = authentication_path(record_levels, record_index)

        secret = self.db.secret_cell(batch_id, record_index, path)
        proof: dict[str, Any] = {
            "protocol_version": "audit-commit-v1",
            "digest": public["digest"],
            "batch_id": batch_id,
            "batch_root_hex": root,
            "record_count": public["record_count"],
            "field_count": public["field_count"],
            "claim": {
                "record_index": record_index,
                "position": field["position"],
                "path": path,
                "field_type": field["field_type"],
                "state": field["state"],
                "commitment_hex": field["commitment_hex"],
            },
            "field_tree": {
                "leaf_count": public["field_count"],
                "siblings_hex": [None if s is None else s.hex() for s in field_sibs],
                "record_root_hex": record["record_root_hex"],
            },
            "record_tree": {
                "leaf_count": public["record_count"],
                "siblings_hex": [None if s is None else s.hex() for s in record_sibs],
            },
        }
        if field["state"] == "present":
            proof["reveal"] = {
                "value": secret["value"],
                "salt_hex": secret["salt_hex"],
            }
        elif field["state"] == "null":
            proof["reveal"] = {"value": None, "salt_hex": secret["salt_hex"]}
        else:
            proof["reveal"] = None

        self.db.insert_audit_event(
            {
                "run_id": self.log.run_id,
                "event_type": "FIELD_DISCLOSED",
                "batch_id": batch_id,
                "verdict": "OK",
                "category": None,
                "fingerprint": fingerprint(
                    (batch_id, record_index, path), public["digest"]
                ),
                "detail": {
                    "record_index": record_index,
                    "path": path,
                    "state": field["state"],
                    "field_type": field["field_type"],
                    # value/salt deliberately excluded
                },
            }
        )
        self.log.step(
            "disclose:issued",
            extra={"state": field["state"], "path": path},
        )
        return proof

    # ----------------------------------------------------------------- verify
    def verify(
        self,
        *,
        proof: dict[str, Any],
        trusted_root_hex: str | None,
        expected_path: str | None,
        expected_record_index: int | None,
    ) -> dict[str, Any]:
        # If the caller omits a trusted root but names a local batch, anchor
        # to the locally stored root. The audit records which anchor applied.
        anchor_source = "caller"
        if trusted_root_hex is None:
            batch_id = proof.get("batch_id") if isinstance(proof, dict) else None
            if not isinstance(batch_id, str):
                raise CoreError(
                    "verify requires trusted_batch_root_hex or a proof with a "
                    "locally known batch_id"
                )
            trusted_root_hex = self.db.batch_root(batch_id)
            anchor_source = "local-store"

        verdict = verify_proof(
            proof,
            trusted_batch_root_hex=trusted_root_hex,
            expected_path=expected_path,
            expected_record_index=expected_record_index,
        )
        self.db.insert_audit_event(
            {
                "run_id": self.log.run_id,
                "event_type": "PROOF_VERIFIED",
                "batch_id": proof.get("batch_id") if isinstance(proof, dict) else None,
                "verdict": "ACCEPT" if verdict.valid else "REJECT",
                "category": verdict.category,
                "fingerprint": fingerprint(
                    (trusted_root_hex, expected_path, expected_record_index),
                    proof.get("digest", "sha256")
                    if isinstance(proof, dict)
                    else "sha256",
                ),
                "detail": {
                    "anchor_source": anchor_source,
                    "checked_steps": list(verdict.checked_steps),
                    "reason": verdict.reason,
                    "expected_path": expected_path,
                    "expected_record_index": expected_record_index,
                },
            }
        )
        self.log.verdict(
            verdict.valid,
            verdict.category,
            verdict.reason,
            extra={"steps": ",".join(verdict.checked_steps)},
        )
        return verdict.to_dict()

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _find_public_record(public: dict[str, Any], record_index: int) -> dict[str, Any]:
        from app.core.errors import RecordNotFound

        for r in public["records"]:
            if r["record_index"] == record_index:
                return r
        raise RecordNotFound(
            f"record {record_index} not found (records=0..{public['record_count']-1})"
        )

    @staticmethod
    def _find_public_field(record: dict[str, Any], path: str) -> dict[str, Any]:
        from app.core.errors import FieldNotCommitted

        for f in record["fields"]:
            if f["path"] == path:
                return f
        raise FieldNotCommitted(
            f"field {path!r} is not committed in record {record['record_index']}"
        )
