"""Independent verifier against concrete attack scenarios.

Packages are produced by the real DisclosureService, then tampered with.
Every case asserts the SPECIFIC failure category — never a generic
non-200 / falsy result.
"""
from __future__ import annotations

import copy
import json

import pytest

from app.config import Settings
from app.services.batch_service import BatchService
from app.services.disclosure_service import DisclosureService
from app.verifier.independent import VerifyStatus, classify_item, verify_package
from tests.fixtures.fixtures import records as fixture_records, schema as fixture_schema


@pytest.fixture()
def package(db, run_id):
    settings = Settings(db_path=db.path, audit_log_path="logs/test.log", salt_bytes=16)
    BatchService(db, settings, run_id).create_batch(
        fixture_schema(), fixture_records(), batch_id="batch-attacks")
    pkg = DisclosureService(db, run_id).issue("batch-attacks", [
        {"record_index": 0, "field_name": "merchant"},
        {"record_index": 0, "field_name": "amount"},
        {"record_index": 0, "field_name": "note"},
        {"record_index": 1, "field_name": "note"},
        {"record_index": 1, "field_name": "status"},
        {"record_index": 2, "field_name": "quantity"},
        {"record_index": 2, "field_name": "note"},
    ])
    return pkg


def _verdict(pkg):
    return verify_package(copy.deepcopy(pkg)).verdict


def test_genuine_package_is_valid(package):
    report = verify_package(copy.deepcopy(package))
    assert report.verdict == VerifyStatus.VALID
    assert report.is_valid is True
    assert report.errors == []
    assert len(report.items) == len(package["disclosed"])
    assert all(i.verdict == VerifyStatus.VALID for i in report.items)
    assert report.root_hex == report.recomputed_root_hex


# ---------------------------------------------------------- acceptance cases
def test_same_value_different_fields_cannot_be_substituted(package):
    # merchant r0 == merchant r1 as strings; attach r1's item under r0 identity
    # is impossible without changing manifest; within a package, swap the
    # disclosed VALUE+SALT between two same-valued but different-position cells.
    by_key = {(i["record_index"], i["field_name"]): i for i in package["disclosed"]}
    item = by_key[(0, "merchant")]
    twin = by_key[(2, "note")]  # note r2 = "7", unrelated; do a direct name swap
    # Move item identity to another field name while keeping value/salt:
    item["field_name"] = "note"
    item["field_position"] = 6
    # Its claimed commitment is bound to merchant in the manifest. Either the
    # duplicate-identity guard (CELL_SET_MISMATCH) or the binding check
    # (FIELD_IDENTITY_MISMATCH) must fire — never VALID.
    assert _verdict(package) in (VerifyStatus.CELL_SET_MISMATCH,
                                 VerifyStatus.FIELD_IDENTITY_MISMATCH)


def test_field_position_swap_rejected(package):
    # quantity r2 and note r2 both carry the characters "7" but typed differently.
    q = next(i for i in package["disclosed"]
             if i["record_index"] == 2 and i["field_name"] == "quantity")
    q["field_position"] = 6  # claim the int lives at the string field slot
    # identity (2, quantity) in manifest points at the original commitment;
    # recompute still matches it (name unchanged), but duplicate identity
    # handling: field_position is not part of manifest key mapping — instead the
    # recomputed commitment changes because position is inside the hash.
    assert _verdict(package) == VerifyStatus.COMMITMENT_MISMATCH


def test_empty_string_vs_null_vs_missing_distinct(package):
    note0 = next(i for i in package["disclosed"]
                 if i["record_index"] == 0 and i["field_name"] == "note")
    note1 = next(i for i in package["disclosed"]
                 if i["record_index"] == 1 and i["field_name"] == "note")
    assert note0["state"] == "present" and note0["value"] == ""
    assert note1["state"] == "null" and note1["value"] is None
    status1 = next(i for i in package["disclosed"]
                   if i["record_index"] == 1 and i["field_name"] == "status")
    assert status1["state"] == "missing" and status1["value"] is None

    # Turning the null cell into an empty string must fail commitment:
    tampered = copy.deepcopy(package)
    n1 = next(i for i in tampered["disclosed"]
              if i["record_index"] == 1 and i["field_name"] == "note")
    n1["state"] = "present"
    n1["value"] = ""
    assert _verdict(tampered) == VerifyStatus.COMMITMENT_MISMATCH

    # Claiming the missing cell is actually null must fail:
    tampered2 = copy.deepcopy(package)
    s1 = next(i for i in tampered2["disclosed"]
              if i["record_index"] == 1 and i["field_name"] == "status")
    s1["state"] = "null"
    assert _verdict(tampered2) == VerifyStatus.COMMITMENT_MISMATCH


def test_wrong_salt_is_classified(package, db, run_id):
    tampered = copy.deepcopy(package)
    item = tampered["disclosed"][0]
    real_salt = item["salt_hex"]
    item["salt_hex"] = "00" * 16
    report = verify_package(tampered)
    assert report.verdict == VerifyStatus.COMMITMENT_MISMATCH
    bad = next(i for i in report.items
               if i.record_index == item["record_index"] and i.field_name == item["field_name"])
    assert bad.detail["recomputed_commitment_hex"] != bad.detail["claimed_commitment_hex"]

    # Reference-assisted diagnosis localises it to the salt:
    leaf = next(l for l in db.list_leaves("batch-attacks")
                if l.record_index == item["record_index"] and l.field_name == item["field_name"])
    reference = {"record_index": leaf.record_index, "field_position": leaf.field_position,
                 "field_name": leaf.field_name, "field_type": leaf.field_type,
                 "state": "present", "commitment_hex": leaf.commitment_hex,
                 "salt_hex": leaf.salt_hex}
    assert classify_item(item, reference) == VerifyStatus.WRONG_SALT
    assert real_salt != "00" * 16


def test_wrong_value_is_classified_distinct_from_wrong_salt(package, db):
    tampered = copy.deepcopy(package)
    item = next(i for i in tampered["disclosed"]
                if i["record_index"] == 0 and i["field_name"] == "amount")
    item["value"] = "99.99"
    assert _verdict(tampered) == VerifyStatus.COMMITMENT_MISMATCH
    leaf = next(l for l in db.list_leaves("batch-attacks")
                if l.record_index == 0 and l.field_name == "amount")
    reference = {"record_index": leaf.record_index, "field_position": leaf.field_position,
                 "field_name": leaf.field_name, "field_type": leaf.field_type,
                 "state": "present", "commitment_hex": leaf.commitment_hex,
                 "salt_hex": leaf.salt_hex}
    assert classify_item(item, reference) == VerifyStatus.WRONG_VALUE


def test_wrong_root_rejected(package):
    tampered = copy.deepcopy(package)
    tampered["root_hex"] = "00" * 32
    report = verify_package(tampered)
    # Every path now fails to reach the (wrong) claimed root.
    assert report.verdict == VerifyStatus.PROOF_INVALID
    assert all(i.verdict == VerifyStatus.PROOF_INVALID for i in report.items)


def test_root_mismatch_via_manifest_tamper(package):
    tampered = copy.deepcopy(package)
    # Mutate an UNDISCLOSED manifest commitment: items still verify against the
    # claimed root via their paths, but the recomputed whole-tree root differs.
    disclosed_keys = {(i["record_index"], i["field_name"]) for i in tampered["disclosed"]}
    for cell in tampered["manifest"]["cells"]:
        if (cell["record_index"], cell["field_name"]) not in disclosed_keys:
            cell["commitment_hex"] = "11" * 32
            break
    report = verify_package(tampered)
    assert report.verdict == VerifyStatus.ROOT_MISMATCH
    assert report.recomputed_root_hex != report.root_hex


def test_corrupted_merkle_path_rejected(package):
    tampered = copy.deepcopy(package)
    tampered["disclosed"][0]["merkle_path"][0]["hash_hex"] = "ab" * 32
    assert _verdict(tampered) == VerifyStatus.PROOF_INVALID


def test_malformed_packages_do_not_return_success(package):
    cases = []
    bad_version = copy.deepcopy(package)
    bad_version["schema_version"] = "audit-disclosure-v9"
    cases.append(bad_version)

    bad_hex = copy.deepcopy(package)
    bad_hex["root_hex"] = "not-hex"
    cases.append(bad_hex)

    not_obj = ["nope"]
    cases.append(not_obj)

    bad_item = copy.deepcopy(package)
    bad_item["disclosed"][0]["field_type"] = "string"  # decimal as string
    # value "12.30" encodes under string without error but changes commitment;
    # to force a malformed encode, make value an object:
    bad_item.disclosed if False else None
    bad_item["disclosed"][0]["value"] = {"nested": 1}
    cases.append(bad_item)

    verdicts = [_verdict(c) for c in cases]
    assert verdicts[0] == VerifyStatus.MALFORMED_PACKAGE
    assert verdicts[1] == VerifyStatus.MALFORMED_PACKAGE
    assert verdicts[2] == VerifyStatus.MALFORMED_PACKAGE
    # Object under a declared string type cannot be canonically encoded.
    assert verdicts[3] == VerifyStatus.MALFORMED_PACKAGE


def test_duplicate_cell_identity_in_manifest_detected(package):
    tampered = copy.deepcopy(package)
    # Clone a manifest cell onto a different leaf index.
    cells = tampered["manifest"]["cells"]
    dup = dict(cells[5])
    cells.insert(6, dup)  # duplicate identity at a new position
    assert _verdict(tampered) in (
        VerifyStatus.CELL_SET_MISMATCH, VerifyStatus.MALFORMED_PACKAGE)


def test_serialisable_report_contains_specific_codes(package):
    tampered = copy.deepcopy(package)
    tampered["disclosed"][1]["salt_hex"] = "ff" * 16
    report = verify_package(tampered).to_dict()
    assert report["valid"] is False
    assert report["verdict"] == "COMMITMENT_MISMATCH"
    json.dumps(report)  # must be fully JSON-serialisable for the API/logs
