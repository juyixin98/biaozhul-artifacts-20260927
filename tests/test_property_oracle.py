"""Property tests: over hundreds of randomized tricky-unicode strings, the
built index must equal the independent regex-oracle tables, and every legal
boundary round-trips through all three position spaces."""

from __future__ import annotations

import random

import pytest

from textindex import index as index_mod

from . import fixtures, oracle

N_RANDOM = 300
SEED = 20260928


@pytest.mark.parametrize("trial", range(N_RANDOM))
def test_random_index_matches_oracle(trial, recorder):
    rec, counts = recorder
    rng = random.Random(SEED + trial)
    text = oracle.random_text(rng, min_len=0, max_len=50)
    idx = index_mod.build_index(text)
    exp = oracle.expected_index(text)
    actual = {
        "cp_starts": list(idx.cp_start),
        "byte_starts": list(idx.byte_start),
        "clusters": idx.clusters(),
        "codepoints": idx.codepoint_count,
        "bytes": idx.byte_count,
    }
    passed = (
        actual["cp_starts"] == exp["cp_starts"]
        and actual["byte_starts"] == exp["byte_starts"]
        and actual["clusters"] == exp["clusters"]
        and actual["codepoints"] == exp["codepoints"]
        and actual["bytes"] == exp["bytes"]
    )
    if trial % 25 == 0 or not passed:
        counts["PASS" if passed else "FAIL"] += 1
        rec.judge(
            test=f"random_oracle[{trial}]", kind="property", passed=passed,
            expected=exp, actual=actual,
            intermediate={"length": len(text),
                          "codepoints": [f"U+{ord(c):04X}" for c in text][:40],
                          # full (untruncated) sequence lets replay rebuild
                          "replay_codepoints":
                              [f"U+{ord(c):04X}" for c in text]},
            reason="our index tables must equal regex \\X + UTF-8 oracle",
        )
    assert passed, f"seed {SEED + trial}: mismatch on {text!r}"


@pytest.mark.parametrize("trial", range(100))
def test_random_roundtrip_conversion(trial):
    rng = random.Random(SEED * 3 + trial)
    text = oracle.random_text(rng, min_len=1, max_len=40)
    idx = index_mod.build_index(text)
    oracle_bytes = oracle.cp_byte_offsets(text)
    oracle_clusters = oracle.cluster_starts(text)
    # every codepoint start converts byte<->cp exactly both directions
    for cp, by in enumerate(oracle_bytes[:-1]):
        assert idx.codepoint_to_byte(cp, strict=False) == by
        assert idx.byte_to_codepoint(by, strict=False) == cp
    # every cluster boundary is accepted strict in every space
    for k, cp in enumerate(oracle_clusters):
        by = oracle_bytes[cp]
        assert idx.codepoint_to_cluster(cp, strict=True) == k
        assert idx.byte_to_cluster(by, strict=True) == k
        assert idx.cluster_to_codepoint(k) == cp
        assert idx.cluster_to_byte(k) == by


def test_oracle_agrees_on_all_hand_fixtures():
    for key, (text, exp) in fixtures.ALL_TEXT_FIXTURES.items():
        o = oracle.expected_index(text)
        assert o["cp_starts"] == exp["cluster_cp_starts"], key
        assert o["byte_starts"] == exp["cluster_byte_starts"], key
        assert o["clusters"] == exp["clusters"], key
