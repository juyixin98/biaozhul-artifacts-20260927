"""Selective disclosure: reveal chosen (record, field) cells with proofs.

A disclosure package contains only the selected cells' salts and values.
Salts/values of every other cell never leave the database — the public
manifest still lists all cell identities so the verifier can confirm the
tree is complete, while undisclosed cells appear as commitments only.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..crypto.merkle import build_merkle_tree
from ..observability import get_logger
from ..storage.db import Database, LeafRow
from ..version import (
    COMMITMENT_SCHEMA_VERSION,
    DISCLOSURE_SCHEMA_VERSION,
    MERKLE_SCHEMA_VERSION,
)

MISSING_VALUE_MARKER = {"__missing__": True}


class DisclosureError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Selector:
    record_index: int
    field_name: str


class DisclosureService:
    def __init__(self, db: Database, run_id: str) -> None:
        self.db = db
        self.run_id = run_id
        self.log = get_logger()

    def issue(self, batch_id: str, selectors_raw: list[dict]) -> dict:
        selectors = _parse_selectors(selectors_raw)
        batch = self.db.get_batch(batch_id)
        if batch is None:
            self.db.append_audit(run_id=self.run_id, action="disclosure.issue",
                                 verdict="BATCH_NOT_FOUND", batch_id=batch_id,
                                 detail={"selectors": selectors_raw})
            raise DisclosureError("BATCH_NOT_FOUND", f"unknown batch: {batch_id}")

        leaves = self.db.list_leaves(batch_id)
        by_identity = {(leaf.record_index, leaf.field_name): leaf for leaf in leaves}

        _, paths = build_merkle_tree([leaf.commitment_hex for leaf in leaves])
        path_by_index = {leaf.leaf_index: paths[leaf.leaf_index] for leaf in leaves}

        # Public manifest: complete identity list, no private material.
        manifest_cells = [
            {
                "leaf_index": leaf.leaf_index,
                "record_index": leaf.record_index,
                "field_position": leaf.field_position,
                "field_name": leaf.field_name,
                "field_type": leaf.field_type,
                "commitment_hex": leaf.commitment_hex,
            }
            for leaf in leaves
        ]

        items: list[dict] = []
        for selector in selectors:
            key = (selector.record_index, selector.field_name)
            leaf = by_identity.get(key)
            if leaf is None:
                self.db.append_audit(
                    run_id=self.run_id, batch_id=batch_id, action="disclosure.issue",
                    verdict="UNKNOWN_FIELD", detail={"selector": selector.__dict__},
                )
                raise DisclosureError(
                    "UNKNOWN_FIELD",
                    f"no such cell: record={selector.record_index} field={selector.field_name!r}",
                )
            items.append(_item_payload(leaf, path_by_index[leaf.leaf_index]))

        package = {
            "schema_version": DISCLOSURE_SCHEMA_VERSION,
            "commitment_schema_version": COMMITMENT_SCHEMA_VERSION,
            "merkle_schema_version": MERKLE_SCHEMA_VERSION,
            "batch_id": batch_id,
            "root_hex": batch.root_hex,
            "manifest": {"cells": manifest_cells, "record_count": batch.record_count,
                         "schema": batch.schema},
            "disclosed": items,
        }
        self.db.append_audit(
            run_id=self.run_id, batch_id=batch_id, action="disclosure.issue",
            verdict="ISSUED",
            detail={"disclosed": [(s.record_index, s.field_name) for s in selectors]},
        )
        self.log.info(
            "disclosure package issued",
            extra={"run_id": self.run_id, "batch_id": batch_id, "step": "disclosure.issue",
                   "verdict": "ISSUED",
                   "detail": {"count": len(items),
                              "cells": [[i["record_index"], i["field_name"]] for i in items]}},
        )
        return package


def _item_payload(leaf: LeafRow, path) -> dict:
    if isinstance(leaf.value, dict) and leaf.value == MISSING_VALUE_MARKER:
        payload: dict = {"state": "missing", "value": None}
    elif leaf.value is None:
        payload = {"state": "null", "value": None}
    else:
        payload = {"state": "present", "value": leaf.value}
    return {
        "record_index": leaf.record_index,
        "field_position": leaf.field_position,
        "field_name": leaf.field_name,
        "field_type": leaf.field_type,
        "salt_hex": leaf.salt_hex,  # may be "" for intentionally unsalted cells
        **payload,
        "leaf_index": leaf.leaf_index,
        "commitment_hex": leaf.commitment_hex,
        "merkle_path": [step.to_dict() for step in path],
    }


def _parse_selectors(raw: list[dict]) -> list[Selector]:
    if not isinstance(raw, list) or not raw:
        raise DisclosureError("MALFORMED_REQUEST", "selectors must be a non-empty list")
    out: list[Selector] = []
    seen: set[tuple[int, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise DisclosureError("MALFORMED_REQUEST", "selector must be an object")
        ri = item.get("record_index")
        name = item.get("field_name")
        if not isinstance(ri, int) or isinstance(ri, bool) or ri < 0:
            raise DisclosureError("MALFORMED_REQUEST", f"bad record_index: {ri!r}")
        if not isinstance(name, str) or not name:
            raise DisclosureError("MALFORMED_REQUEST", f"bad field_name: {name!r}")
        key = (ri, name)
        if key in seen:
            raise DisclosureError("MALFORMED_REQUEST", f"duplicate selector: {key}")
        seen.add(key)
        out.append(Selector(record_index=ri, field_name=name))
    return out
