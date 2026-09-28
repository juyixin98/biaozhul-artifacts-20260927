"""Tests that pin the COMMITTED sample artifacts under data/sample/.

These guards ensure the checked-in journal/manifest are genuine artifacts:
the journal replays to the manifest root with the committed HMAC key, and
every sample proof verifies.
"""
from __future__ import annotations

import json
import os

import pytest

from smt.kernel import MemoryNodeStore, verify_proof
from smt.services import load_journal_file, replay_records

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "data", "sample")
DEV_KEY = "dev-only-journal-key-change-me"


def _load(name):
    with open(os.path.join(SAMPLE, name), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.mark.skipif(not os.path.isdir(SAMPLE), reason="sample data not generated")
def test_sample_journal_replays_to_manifest_root():
    manifest = _load("manifest.json")
    records = load_journal_file(os.path.join(SAMPLE, "journal.json"))
    outcome = replay_records(records, MemoryNodeStore(), DEV_KEY)
    assert outcome.root_matches
    assert outcome.final_root.hex() == manifest["final_root"]
    assert manifest["final_equals_batch_root"] is True


@pytest.mark.skipif(not os.path.isdir(SAMPLE), reason="sample data not generated")
@pytest.mark.parametrize("proof_name", [
    "membership_ka", "nonmembership_in_subtree", "nonmembership_elsewhere",
])
def test_sample_manifest_proofs_verify(proof_name):
    manifest = _load("manifest.json")
    proof = manifest["sample_proofs"][proof_name]
    result = verify_proof(proof)
    assert result.ok, f"{proof_name}: {result.verdict.value}: {result.reason}"


@pytest.mark.skipif(not os.path.isdir(SAMPLE), reason="sample data not generated")
def test_sample_stages_show_delete_restore():
    stages = {s["stage"]: s["root"] for s in _load("manifest.json")["stages"]}
    assert stages["after_batch"] == stages["after_reinsert_ka"]
    assert stages["after_batch"] != stages["after_delete_ka"]
    assert stages["empty"] != stages["after_batch"]
