"""Privacy boundaries and the documented low-entropy limitation.

  * salts and undisclosed raw values must never appear in public responses or
    in disclosure packages for unselected cells;
  * UNSALTED commitments of low-entropy values are shown to be enumerable
    (the explicit limitation), while salted commitments of the same value
    resist the same dictionary attack.
"""
from __future__ import annotations

import copy
import json

from app.config import Settings
from app.crypto.commitment import CommitmentInput, commit_field
from app.domain.types import FieldType, canonical_encode
from app.services.batch_service import BatchService
from app.services.disclosure_service import DisclosureService
from tests.fixtures.fixtures import records as fixture_records, schema as fixture_schema


def _setup(db, run_id):
    settings = Settings(db_path=db.path, audit_log_path="logs/test.log", salt_bytes=16)
    BatchService(db, settings, run_id).create_batch(
        fixture_schema(), fixture_records(), batch_id="batch-privacy")
    return DisclosureService(db, run_id)


def test_public_views_contain_no_salt_or_value(db, run_id):
    service = _setup(db, run_id)
    batch = db.get_batch("batch-privacy")
    view = BatchService(db, Settings(db_path=db.path, audit_log_path="logs/x.log"),
                        run_id).public_batch_view(batch, db.list_leaves("batch-privacy"))
    serialized = json.dumps(view)
    # Schema may carry the boolean flag "salted"; the secret material itself
    # (any salt hex string, any raw value) must be absent.
    assert "salt_hex" not in serialized
    assert '"value"' not in serialized
    assert "Blue Kiosk" not in serialized
    assert "12.30" not in serialized
    for leaf in db.list_leaves("batch-privacy"):
        if leaf.salt_hex:
            assert leaf.salt_hex not in serialized


def test_disclosure_exposes_only_selected_cells(db, run_id):
    service = _setup(db, run_id)
    pkg = service.issue("batch-privacy", [{"record_index": 0, "field_name": "merchant"}])
    blob = json.dumps(pkg)

    # Selected value/salt present.
    assert pkg["disclosed"][0]["value"] == "Blue Kiosk"
    assert len(pkg["disclosed"][0]["salt_hex"]) == 32

    # Undisclosed values/salts absent from the package entirely.
    assert "12.30" not in blob
    assert "Cafe Seven" not in blob
    # All salts except the single disclosed one must not be in the blob.
    leaves = db.list_leaves("batch-privacy")
    disclosed_salt = pkg["disclosed"][0]["salt_hex"]
    for leaf in leaves:
        if leaf.salt_hex and leaf.salt_hex != disclosed_salt:
            assert leaf.salt_hex not in blob


def test_manifest_lists_all_identities_but_no_private_material(db, run_id):
    service = _setup(db, run_id)
    pkg = service.issue("batch-privacy", [{"record_index": 0, "field_name": "amount"}])
    assert len(pkg["manifest"]["cells"]) == 3 * 8
    for cell in pkg["manifest"]["cells"]:
        assert "salt_hex" not in cell and "value" not in cell
        assert set(cell) == {"leaf_index", "record_index", "field_position",
                             "field_name", "field_type", "commitment_hex"}


def test_low_entropy_unsalted_commitment_is_dictionary_enumerable(db, run_id):
    """The documented limitation: no salt => guess-and-check works."""
    candidates = ["PENDING", "PAID", "REJECTED", "VOID", "DRAFT"]
    service = _setup(db, run_id)
    pkg = service.issue("batch-privacy", [{"record_index": 0, "field_name": "status"}])
    item = pkg["disclosed"][0]
    assert item["salt_hex"] == ""  # unsalted by schema
    encoded = canonical_encode(FieldType.STRING, "PAID")

    # Attacker enumerates the small candidate space with the public identity.
    found = None
    for guess in candidates:
        c = commit_field(CommitmentInput(
            record_index=item["record_index"], field_position=item["field_position"],
            field_name=item["field_name"],
            encoded_value=canonical_encode(FieldType.STRING, guess),
            salt=b"")).commitment_hex
        if c == item["commitment_hex"]:
            found = guess
            break
    assert found == "PAID"  # enumerable: this is the KNOWN limitation

    # Batch warnings must advertise the limitation publicly.
    batch = db.get_batch("batch-privacy")
    assert any("UNSALTED_FIELD_ENUMERABLE" in w and "status" in w for w in batch.warnings)

    # And a SALTED version of the same low-entropy value resists enumeration:
    salted_commit = commit_field(CommitmentInput(
        record_index=0, field_position=item["field_position"], field_name="status",
        encoded_value=encoded, salt=bytes.fromhex("01" * 16))).commitment_hex
    assert all(
        commit_field(CommitmentInput(
            record_index=0, field_position=item["field_position"], field_name="status",
            encoded_value=canonical_encode(FieldType.STRING, guess), salt=b"")).commitment_hex
        != salted_commit
        for guess in candidates
    )
