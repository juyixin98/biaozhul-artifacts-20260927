"""Property-style tests: random synthetic inputs vs the independent oracle.

The expected results here come exclusively from tests/oracle.py, which
shares no production code with dictsvc.core. A fixed seed makes every run
reproducible; a failure prints the exact input and run identity.
"""
from __future__ import annotations

import random

import pytest

from dictsvc.core import encode_run, verify_roundtrip

from .conftest import to_batch_input
from .oracle import (
    decode_ref_encoding, expected_decoded_rows, expected_encoding,
)

ALPHABET = ["a", "b", "m", "n", "z", "1", "10", "aa", ""]
INTS = [0, 1, 2, 10, 100, -1, 255, 256]


def _random_batch(rng, bid):
    n_distinct = rng.randint(0, 6)
    distinct = [rng.choice(
        rng.choice([ALPHABET, INTS])) for _ in range(n_distinct)]
    # Build the declared dictionary, deliberately inserting repeats.
    declared = list(distinct)
    for _ in range(rng.randint(0, 3)):
        if distinct:
            declared.insert(rng.randint(0, len(declared)),
                            rng.choice(distinct))
    rows = rng.randint(0, 8)
    indices, valid = [], []
    for _ in range(rows):
        if rng.random() < 0.3:
            indices.append(rng.randint(-5, 5))  # slot ignored when invalid
            valid.append(False)
        elif declared:
            indices.append(rng.randrange(len(declared)))
            valid.append(True)
        else:
            indices.append(0)
            valid.append(False)
    from .oracle import RefBatch
    return RefBatch(bid, tuple(declared), tuple(indices), tuple(valid))


@pytest.mark.parametrize("seed", range(40))
def test_random_runs_match_oracle_and_roundtrip(seed):
    rng = random.Random(20260928 + seed)
    refs = [_random_batch(rng, f"batch-{i}") for i in
            range(rng.randint(1, 4))]
    width = rng.choice([8, 16])
    try:
        ref = expected_encoding(refs, target_width=width)
    except OverflowError:
        # Oracle says overflow: the kernel must classify it identically
        # under reject, and succeed under expand with the oracle width.
        from dictsvc.core import CardinalityOverflow
        with pytest.raises(CardinalityOverflow):
            encode_run([to_batch_input(b) for b in refs],
                       target_width=width, width_policy="reject")
        ref = expected_encoding(refs, target_width=width,
                                width_policy="expand")
        enc = encode_run([to_batch_input(b) for b in refs],
                         target_width=width, width_policy="expand")
    else:
        enc = encode_run([to_batch_input(b) for b in refs],
                         target_width=width)

    identity = f"seed={seed}"
    assert enc.global_values == ref.global_values, identity
    assert enc.global_types == ref.global_types, identity
    assert enc.global_index_width == ref.width, identity
    for rb in enc.batches:
        exp_idx, exp_valid = ref.remapped[rb.batch_id]
        assert tuple(rb.global_indices) == exp_idx, identity
        assert tuple(rb.valid) == exp_valid, identity
        assert rb.local_to_global == ref.local_to_global[rb.batch_id], \
            identity

    # Decode via the kernel and via the oracle must equal original rows.
    report = verify_roundtrip(
        enc, {b.batch_id: to_batch_input(b) for b in refs})
    assert report.all_match, (identity, report.mismatches)
    assert decode_ref_encoding(ref) == expected_decoded_rows(refs), identity


def test_merge_invariants_on_random_pairs():
    """Same value/different local code -> SAME global code; same local
    code/different value -> DIFFERENT global codes."""
    rng = random.Random(424242)
    for _ in range(30):
        v = rng.choice(ALPHABET + INTS)
        w = rng.choice([x for x in ALPHABET + INTS if x != v])
        from .oracle import RefBatch
        b1 = RefBatch("b1", (v, w, v), (0, 2), (True, True))
        b2 = RefBatch("b2", (w, v), (0, 1), (True, True))
        enc = encode_run([to_batch_input(b1), to_batch_input(b2)])
        m1 = enc.batches[0].local_to_global
        m2 = enc.batches[1].local_to_global
        # repeated value in b1 merges
        assert m1[0] == m1[2]
        # b1 code0 (v) and b2 code0 (w) must not collide
        assert m1[0] != m2[0]
        # shared value v: b1 code0 and b2 code1 are the same global code
        assert m1[0] == m2[1]
