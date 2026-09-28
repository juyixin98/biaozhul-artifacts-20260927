"""Independent checker re-validates detector-produced evidence and tamper cases.

The checker (``independent`` package) shares no code with ``ffg_slash``; it
re-implements encoding, Ed25519 verification, snapshot roots and the offense
rules from docs/protocol.md.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from independent.checker import review

from .conftest import make_vote


def _evidence(svc, votes_and_keys):
    """Run votes through the detector and return the last evidence packet."""
    evidence = None
    for seed, pub, vote in votes_and_keys:
        r = svc.ingest(vote)
        if r.evidences:
            evidence = r.evidences[-1].packet
    assert evidence is not None
    return evidence


def test_independent_checker_accepts_real_double_vote(standard_service, keys):
    svc = standard_service
    seed, pub = keys["alpha"]
    v1 = make_vote(seed, pub, source_epoch=1, target_epoch=2,
                   target_root=b"\xAA" * 32)
    v2 = make_vote(seed, pub, source_epoch=1, target_epoch=2,
                   target_root=b"\xBB" * 32)
    packet = _evidence(svc, [(seed, pub, v1), (seed, pub, v2)])

    verdict = review(copy.deepcopy(packet))
    assert verdict.valid is True
    assert verdict.reason == "valid"
    assert verdict.offense == "double_vote"
    # checker reports the concrete steps it performed
    assert any("signature" in c for c in verdict.checks)
    assert any("evidence_id" in c for c in verdict.checks)


def test_independent_checker_accepts_real_surround(keys, service_factory):
    svc = service_factory({e: ["alpha", "bravo", "charlie", "delta"]
                           for e in range(1, 7)})
    seed, pub = keys["alpha"]
    outer = make_vote(seed, pub, source_epoch=1, target_epoch=6)
    inner = make_vote(seed, pub, source_epoch=2, target_epoch=5)
    packet = _evidence(svc, [(seed, pub, outer), (seed, pub, inner)])

    verdict = review(packet)
    assert verdict.valid is True
    assert verdict.offense == "surround_vote"


@pytest.mark.parametrize("mutation,expected_reason", [
    # flip a signature byte -> crypto check fails BEFORE any rule check
    (lambda p: p["vote_2"].__setitem__("signature", "00" * 64), "bad_signature"),
    # change target root only in packet content but keep old signature
    (lambda p: p["vote_2"].__setitem__("target_root", "CD" * 32), "bad_signature"),
    # swap offense type on a valid surround packet -> rule mismatch
    (None, "rule_mismatch"),
    # remove the validator from its own target snapshot -> member list no
    # longer matches the committed snapshot root
    (lambda p: p["vote_1_snapshot"].__setitem__(
        "members", [m for m in p["vote_1_snapshot"]["members"]
                    if m["pubkey"] != p["validator_pubkey"]]), "bad_snapshot_root"),
    # a packet with an authentic snapshot but a validator added by the attacker
    # would also fail the root; here swap the signer pubkey -> domain mismatch
    (lambda p: p.__setitem__(
        "validator_pubkey", "cd" * 32), "bad_domain"),
    # tamper with a weight inside the snapshot -> root recomputation catches it
    (lambda p: p["vote_1_snapshot"]["members"][0].__setitem__("weight", 99),
     "bad_snapshot_root"),
    # tamper a vote field so ordering breaks -> structurally invalid first,
    # and its stored signature would also no longer verify
    (lambda p: p["vote_1"].__setitem__("source_epoch", 99), "invalid_format"),
])
def test_independent_checker_rejects_tampering(
        standard_service, keys, mutation, expected_reason):
    svc = standard_service
    seed, pub = keys["alpha"]
    v1 = make_vote(seed, pub, source_epoch=1, target_epoch=2,
                   target_root=b"\xAA" * 32)
    v2 = make_vote(seed, pub, source_epoch=1, target_epoch=2,
                   target_root=b"\xBB" * 32)
    packet = _evidence(svc, [(seed, pub, v1), (seed, pub, v2)])
    packet = copy.deepcopy(packet)

    if mutation is None:
        packet["type"] = "surround_vote"
    else:
        mutation(packet)

    verdict = review(packet)
    assert verdict.valid is False
    assert verdict.reason == expected_reason


def test_malformed_and_unknown_packets_have_named_reasons():
    assert review(b"not json").reason == "invalid_format"
    assert review({}).reason == "unknown_type"
    assert review({"version": 99, "type": "double_vote"}).reason == "unknown_type"
    verdict = review({"version": 1, "type": "mystery",
                      "evidence_id": "ab"})
    assert verdict.reason == "unknown_type"


def test_checker_cli_exit_codes(tmp_path, standard_service, keys):
    svc = standard_service
    seed, pub = keys["alpha"]
    v1 = make_vote(seed, pub, source_epoch=1, target_epoch=2,
                   target_root=b"\xAA" * 32)
    v2 = make_vote(seed, pub, source_epoch=1, target_epoch=2,
                   target_root=b"\xBB" * 32)
    packet = _evidence(svc, [(seed, pub, v1), (seed, pub, v2)])
    good = tmp_path / "good.json"
    good.write_text(json.dumps(packet))
    bad = tmp_path / "bad.json"
    tampered = copy.deepcopy(packet)
    tampered["vote_2"]["signature"] = "00" * 64
    bad.write_text(json.dumps(tampered))

    repo_root = Path(__file__).resolve().parents[1]
    r_ok = subprocess.run(
        [sys.executable, "-m", "independent.cli", str(good)],
        cwd=repo_root, capture_output=True, text=True)
    assert r_ok.returncode == 0, r_ok.stderr
    assert "VALID" in r_ok.stdout

    r_bad = subprocess.run(
        [sys.executable, "-m", "independent.cli", str(bad)],
        cwd=repo_root, capture_output=True, text=True)
    assert r_bad.returncode == 1
    assert "bad_signature" in r_bad.stdout
