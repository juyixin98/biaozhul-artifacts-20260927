"""Batch orchestration: typed encoding -> salted commitments -> Merkle root.

This layer wires the rule layer (domain), the security kernel (crypto) and the
state layer (storage) together. It contains no hashing logic of its own.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from ..config import Settings
from ..crypto.merkle import build_merkle_tree
from ..crypto.commitment import CommitmentInput, commit_field, mint_salt
from ..domain.schema import BatchSchema, FieldSpec, parse_records
from ..domain.types import canonical_encode, canonical_encode_missing
from ..observability import get_logger
from ..storage.db import Database, LeafRow
from ..version import COMMITMENT_SCHEMA_VERSION, MERKLE_SCHEMA_VERSION


def new_batch_id() -> str:
    return "batch-" + secrets.token_hex(8)


@dataclass(frozen=True)
class CreatedCell:
    leaf_index: int
    record_index: int
    field_position: int
    field_name: str
    field_type: str
    salted: bool
    salt_hex: str
    value: object
    encoded_hex: str
    commitment_hex: str


@dataclass(frozen=True)
class CreatedBatch:
    batch_id: str
    root_hex: str
    record_count: int
    schema: list[dict]
    cells: list[CreatedCell]
    warnings: list[str]


def _build_cells(schema: BatchSchema, records: list[dict], salt_bytes: int) -> list[CreatedCell]:
    cells: list[CreatedCell] = []
    leaf_index = 0
    for record_index, record in enumerate(records):
        for position, spec in enumerate(schema.fields):
            if spec.name in record:
                raw_value = record[spec.name]
                encoded = canonical_encode(spec.type, raw_value)
            else:
                # Absent cell: committed explicitly so a holder cannot drop a
                # field without changing the root.
                raw_value = None
                encoded = canonical_encode_missing()
            if spec.salted:
                salt_hex = mint_salt(salt_bytes)
            else:
                salt_hex = ""
            commitment = commit_field(
                CommitmentInput(
                    record_index=record_index,
                    field_position=position,
                    field_name=spec.name,
                    encoded_value=encoded,
                    salt=bytes.fromhex(salt_hex),
                )
            ).commitment_hex
            cells.append(
                CreatedCell(
                    leaf_index=leaf_index,
                    record_index=record_index,
                    field_position=position,
                    field_name=spec.name,
                    field_type=spec.type.value,
                    salted=spec.salted,
                    salt_hex=salt_hex,
                    # Marker distinct from both null and an empty string.
                    value={"__missing__": True} if spec.name not in record else raw_value,
                    encoded_hex=encoded.hex(),
                    commitment_hex=commitment,
                )
            )
            leaf_index += 1
    return cells


def _collect_warnings(schema: BatchSchema, records: list[dict]) -> list[str]:
    warnings: list[str] = []
    unsalted = [s for s in schema.fields if not s.salted]
    for spec in unsalted:
        warnings.append(
            f"UNSALTED_FIELD_ENUMERABLE: field '{spec.name}' (type={spec.type.value}) is "
            "committed without a salt; its commitment is dictionary-enumerable for "
            "low-entropy values"
        )
    missing_cells = sum(1 for rec in records for spec in schema.fields if spec.name not in rec)
    if missing_cells:
        warnings.append(
            f"MISSING_FIELDS_COMMITTED: {missing_cells} absent cell(s) are committed as "
            "explicit 'missing' markers; absence is therefore detectable by a verifier"
        )
    return warnings


class BatchService:
    def __init__(self, db: Database, settings: Settings, run_id: str) -> None:
        self.db = db
        self.settings = settings
        self.run_id = run_id
        self.log = get_logger()

    def create_batch(self, schema_raw: list[dict], records_raw: object,
                     batch_id: str | None = None) -> CreatedBatch:
        schema = BatchSchema.from_dicts(schema_raw)
        records = parse_records(records_raw)
        batch_id = batch_id or new_batch_id()
        self.log.info(
            "creating batch",
            extra={"run_id": self.run_id, "batch_id": batch_id, "step": "batch.create.begin",
                   "detail": {"records": len(records), "fields": len(schema.fields),
                              "commitment_version": COMMITMENT_SCHEMA_VERSION,
                              "merkle_version": MERKLE_SCHEMA_VERSION}},
        )

        cells = _build_cells(schema, records, self.settings.salt_bytes)
        root_hex, _paths = build_merkle_tree([c.commitment_hex for c in cells])
        warnings = _collect_warnings(schema, records)

        self.db.insert_batch(
            batch_id=batch_id,
            schema=schema.to_dicts(),
            root_hex=root_hex,
            leaves=(
                {
                    "leaf_index": c.leaf_index,
                    "record_index": c.record_index,
                    "field_position": c.field_position,
                    "field_name": c.field_name,
                    "field_type": c.field_type,
                    "salted": c.salted,
                    "salt_hex": c.salt_hex,
                    "value": c.value,
                    "encoded_hex": c.encoded_hex,
                    "commitment_hex": c.commitment_hex,
                }
                for c in cells
            ),
            warnings=warnings,
        )
        self.db.append_audit(
            run_id=self.run_id,
            batch_id=batch_id,
            action="batch.create",
            verdict="CREATED",
            detail={"record_count": len(records), "cell_count": len(cells),
                    "root": root_hex, "warnings": warnings},
        )
        self.log.info(
            "batch committed",
            extra={"run_id": self.run_id, "batch_id": batch_id, "step": "batch.create.done",
                   "verdict": "CREATED", "detail": {"root": root_hex, "cells": len(cells)}},
        )
        return CreatedBatch(
            batch_id=batch_id,
            root_hex=root_hex,
            record_count=len(records),
            schema=schema.to_dicts(),
            cells=cells,
            warnings=warnings,
        )

    @staticmethod
    def public_batch_view(batch, leaves: list[LeafRow]) -> dict:
        """Public view: identities and commitments only, never salts/values."""
        return {
            "batch_id": batch.batch_id,
            "schema": batch.schema,
            "record_count": batch.record_count,
            "root_hex": batch.root_hex,
            "warnings": batch.warnings,
            "commitments": [
                {
                    "leaf_index": leaf.leaf_index,
                    "record_index": leaf.record_index,
                    "field_position": leaf.field_position,
                    "field_name": leaf.field_name,
                    "field_type": leaf.field_type,
                    "commitment_hex": leaf.commitment_hex,
                }
                for leaf in leaves
            ],
        }
