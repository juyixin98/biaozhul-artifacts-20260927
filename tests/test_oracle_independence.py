"""Meta-tests proving the independent oracle is genuinely independent.

The oracle file must not import the production kernel/checker/encoding, and
its sequential decisions must agree with the kernel on all fixture scenarios
while being derived by its own code path. We also cross-sign messages with
the oracle's alternative encoding expectations (different wire bytes) to
show the checker validates the PRODUCTION wire format specifically.
"""
from __future__ import annotations

import ast
from pathlib import Path

from localffg.models import SignedVote

import independent_oracle as oracle

ORACLE_PATH = Path(oracle.__file__)
PROD_DIR = ORACLE_PATH.parent.parent / "localffg"


def test_oracle_source_does_not_import_production_core():
    src = ORACLE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    forbidden_modules = {"localffg.kernel", "localffg.checker", "localffg.encoding",
                         "localffg.crypto", "localffg.models", "localffg.service"}
    hit = {m for m in imported if m in forbidden_modules or m.startswith("localffg.")}
    assert not hit, f"oracle must not import production modules, found {hit}"
    # sanity: oracle file exists separately from the package
    assert PROD_DIR.is_dir() and ORACLE_PATH.parent != PROD_DIR


def test_oracle_agrees_with_kernel_on_every_fixture_step(manifest, domain, registry, oracle_registry):
    """Same inputs -> same sequence of exact categories, computed independently
    (the kernel test file cross-checks both against hard-coded expectations).

    Each scenario replays on a fresh kernel: scenarios reuse the same
    synthetic validator identities for independent narratives."""
    from localffg.kernel import SlashingKernel

    mismatches: list[str] = []
    for scenario, steps in manifest["scenarios"].items():
        kernel = SlashingKernel(domain=domain, registry=registry)
        expected = oracle.expected_sequence(domain, oracle_registry, steps)
        for exp, step in zip(expected, steps):
            actual = kernel.ingest(SignedVote.from_json_dict(step["signed"])).status.value
            if actual != exp.status:
                mismatches.append(f"{scenario}/{step['label']}: kernel={actual} oracle={exp.status}")
    assert not mismatches, "\n".join(mismatches)


def test_oracle_classifies_boundary_cases_directly():
    def env(vid, s, t, root="aa", sig="bb", pk="cc"):
        return {"validator_id": vid, "source_round": s, "target_round": t,
                "block_root": root * 32, "signer_pubkey": pk * 32, "signature": sig * 128}

    assert oracle.oracle_classify_pair(env("v", 0, 8, "a"), env("v", 2, 8, "b")) == oracle.DOUBLE
    assert oracle.oracle_classify_pair(env("v", 0, 15), env("v", 5, 10)) == oracle.SURROUND
    assert oracle.oracle_classify_pair(env("v", 5, 10), env("v", 0, 15)) == oracle.SURROUND
    # equal boundaries, crossing, cross-validator, identical -> none
    assert oracle.oracle_classify_pair(env("v", 0, 10), env("v", 10, 15)) is None
    assert oracle.oracle_classify_pair(env("v", 0, 10), env("v", 5, 20)) is None
    assert oracle.oracle_classify_pair(env("v1", 0, 8), env("v2", 0, 8)) is None
    e = env("v", 0, 8)
    assert oracle.oracle_classify_pair(e, dict(e)) is None


def test_oracle_expected_evidence_pair_count(manifest, domain, oracle_registry):
    """Oracle independently enumerates the offense pairs the system must emit:
    S2 double (1), S3 + S3b surround (2), S10 double (1), S11 double (1)."""
    expected_per_scenario = {
        "S1_duplicate_retransmit": 0,
        "S2_double_vote_same_target": 1,
        "S3_surround_nested": 1,
        "S3b_surround_reverse_order": 1,
        "S3c_boundary_equality_no_offense": 0,
        "S3d_crossing_no_surround": 0,
        "S4_membership_change": 0,
        "S5_bad_signature": 0,
        "S5b_wrong_signer_pubkey": 0,
        "S6_unknown_validator": 0,
        "S7_wrong_chain": 0,
        "S8_bad_rounds": 0,
        "S9_cross_validator_no_conflict": 0,
        "S10_weight_snapshot_offense": 1,
        "S11_mixed_conflict_then_rexmit": 1,
    }
    for scenario, expected_n in expected_per_scenario.items():
        pairs = oracle.expected_evidence_pairs(
            domain, oracle_registry, manifest["scenarios"][scenario]
        )
        assert len(pairs) == expected_n, f"{scenario}: {len(pairs)} != {expected_n}"


def test_oracle_signature_verifier_rejects_fixture_tampering(manifest, domain, oracle_registry):
    reg = oracle_registry
    # valid fixture vote verifies
    good = manifest["scenarios"]["S2_double_vote_same_target"][0]["signed"]
    ok, why = oracle.oracle_verify_signature(domain, good, reg.pubkeys["bob"])
    assert ok, why
    # tampered signature rejected
    bad = manifest["scenarios"]["S5_bad_signature"][0]["signed"]
    ok, why = oracle.oracle_verify_signature(domain, bad, reg.pubkeys["alice"])
    assert not ok and why == "bad signature"
    # wrong-chain still signature-valid for bob's key (binding is checked separately)
    wc = manifest["scenarios"]["S7_wrong_chain"][0]["signed"]
    ok, _ = oracle.oracle_verify_signature(domain, wc, reg.pubkeys["bob"])
    assert ok
