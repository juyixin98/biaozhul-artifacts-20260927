"""Local synthetic fixtures: all data is generated here, no external inputs."""
from __future__ import annotations

import random

import pytest


@pytest.fixture
def rng():
    # Fixed seed for reproducibility; tests may reseed via rng.seed(...).
    return random.Random(20260928)


def make_batch(batch_id: str, dictionary, indices, validity=None) -> dict:
    return {
        "batch_id": batch_id,
        "dictionary": list(dictionary),
        "indices": list(indices),
        "validity": None if validity is None else list(validity),
    }


@pytest.fixture
def overlapping_batches():
    """Two batches whose same values use *different* local codes, and which
    share a local code for *different* values:

        b0: code 0='a', code 1='b'
        b1: code 0='b', code 1='c', code 2='a'
    Sorted global dict -> ['a'(0), 'b'(1), 'c'(2)].
    Includes NULL rows.
    """
    b0 = make_batch(
        "b0", ["a", "b"], [0, 1, 0, 1, 0],
        [True, True, False, True, True],
    )
    b1 = make_batch(
        "b1", ["b", "c", "a"], [0, 1, 2, 0, 2, 1],
        [True, True, True, False, True, False],
    )
    return [b0, b1]


@pytest.fixture
def duplicate_dict_batch():
    # local codes 1 and 3 are duplicates of code 0 ('x'); code 2 is distinct.
    return make_batch("dup", ["x", "x", "y", "x"], [0, 1, 2, 3, 2, 0])


@pytest.fixture
def empty_dictionary_batch():
    # no values at all; validity makes every row NULL -> indices must be [0..]
    return make_batch("nulls", [], [0, 0, 0, 0],
                      [False, False, False, False])


@pytest.fixture
def width_batches():
    """Batches spanning the uint8/uint16 boundary with disjoint value sets."""

    def vals(n, prefix):
        return [f"{prefix}{i}" for i in range(n)]

    b0 = make_batch("w0", vals(200, "a"), list(range(200)))
    b1 = make_batch("w1", vals(60, "b"), list(range(60)))
    return [b0, b1]  # cardinality 260 -> uint16
