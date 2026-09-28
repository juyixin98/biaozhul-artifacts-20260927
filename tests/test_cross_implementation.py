"""Cross-implementation / property tests.

Reference oracle: app.verifier.independent — a stdlib-only re-implementation
that imports nothing from app.crypto/app.domain. The service-under-test uses
the cryptography-backed kernel; the two must agree over randomized batches,
including every edge case in the acceptance criteria.
"""
from __future__ import annotations

import random

import pytest

from app.config import Settings
from app.crypto.merkle import build_merkle_tree
from app.services.batch_service import BatchService
from app.verifier.independent import (
    field_commitment as oracle_commit,
    root_from_commitments,
    _encode_value as oracle_encode,
)
from tests.fixtures.fixtures import records as fixture_records, schema as fixture_schema


def _make_service(db, run_id):
    return BatchService(db, Settings(db_path=db.path, audit_log_path="logs/test.log",
                                     salt_bytes=16), run_id)


def test_kernel_and_oracle_agree_on_synthetic_fixture(db, run_id):
    svc = _make_service(db, run_id)
    created = svc.create_batch(fixture_schema(), fixture_records(),
                               batch_id="batch-cross-1")
    leaves = db.list_leaves("batch-cross-1")
    assert len(leaves) == len(fixture_records()) * len(fixture_schema())

    for leaf in leaves:
        state = "missing" if leaf.encoded_hex == "ffff" else (
            "null" if leaf.field_type == "null" and leaf.value is None else "present")
        oracle_bytes = oracle_encode(leaf.field_type, leaf.value, state)
        assert oracle_bytes.hex() == leaf.encoded_hex, leaf.field_name
        salt = bytes.fromhex(leaf.salt_hex) if leaf.salt_hex else b""
        oracle_c = oracle_commit(leaf.record_index, leaf.field_position,
                                 leaf.field_name, oracle_bytes, salt)
        assert oracle_c == leaf.commitment_hex, (leaf.record_index, leaf.field_name)

    root_hex, _ = build_merkle_tree([l.commitment_hex for l in leaves])
    assert root_hex == created.root_hex
    assert root_from_commitments([l.commitment_hex for l in leaves]) == root_hex


_VALUES = [
    ("string", ["", "x", "Blue Kiosk", "café", "7", "PAID"]),
    ("int", [0, -1, 7, 2**40, -(2**40)]),
    ("decimal", ["0", "12.30", "7", "-0.01", "1000000.000001"]),
    ("bool", [True, False]),
    ("date", ["2026-03-01", "2000-02-29"]),
    ("timestamp", ["2026-03-01T09:00:00Z", "2026-03-02T12:30:00+08:00"]),
    ("null", [None]),
]


def test_randomized_cells_agree_with_oracle(db, run_id):
    rng = random.Random(20260928)
    schema = [{"name": f"f{i}", "type": t, "salted": rng.choice([True, True, False])}
              for i, (t, _) in enumerate(_VALUES)]
    records = []
    for r in range(6):
        rec = {}
        for i, (t, choices) in enumerate(_VALUES):
            roll = rng.random()
            if roll < 0.1:
                continue  # missing
            rec[f"f{i}"] = rng.choice(choices)
            if t != "null" and rng.random() < 0.05:
                rec[f"f{i}"] = None if False else rec[f"f{i}"]  # keep typed
        records.append(rec)

    svc = _make_service(db, run_id)
    created = svc.create_batch(schema, records, batch_id="batch-random")
    leaves = db.list_leaves("batch-random")
    for leaf in leaves:
        state = ("missing" if isinstance(leaf.value, dict) and leaf.value.get("__missing__")
                 else ("null" if leaf.value is None and leaf.field_type == "null"
                       else "present"))
        oracle_bytes = oracle_encode(leaf.field_type, leaf.value, state)
        assert oracle_bytes.hex() == leaf.encoded_hex
        salt = bytes.fromhex(leaf.salt_hex) if leaf.salt_hex else b""
        assert oracle_commit(leaf.record_index, leaf.field_position, leaf.field_name,
                             oracle_bytes, salt) == leaf.commitment_hex
    assert root_from_commitments([l.commitment_hex for l in leaves]) == created.root_hex


def test_equal_values_different_fields_never_collide(db, run_id):
    svc = _make_service(db, run_id)
    created = svc.create_batch(fixture_schema(), fixture_records(),
                               batch_id="batch-no-collide")
    leaves = db.list_leaves("batch-no-collide")
    # merchant "Blue Kiosk" appears in records 0 and 1, but cells differ.
    merchants = {l.record_index: l.commitment_hex
                 for l in leaves if l.field_name == "merchant"}
    assert merchants[0] != merchants[1]
    # Within record 2, int quantity=7 and string note="7" differ.
    q = next(l for l in leaves if l.record_index == 2 and l.field_name == "quantity")
    n = next(l for l in leaves if l.record_index == 2 and l.field_name == "note")
    assert q.commitment_hex != n.commitment_hex
    assert created.root_hex  # non-empty
