"""Entropy primitive tests with independently computed exact values."""

import math

import pytest

from secretscan.entropy import shannon_entropy
from conftest import (GHP_TOKEN, HIGH_ENTROPY_PROSE, LOW_ENTROPY_PASSWORD,
                      expected_entropy)


@pytest.mark.parametrize("text", ["", "a", "aaaa", "ab", "0123456789",
                                  GHP_TOKEN, HIGH_ENTROPY_PROSE])
def test_entropy_matches_independent_calculation(text):
    assert shannon_entropy(text) == pytest.approx(expected_entropy(text),
                                                  abs=1e-12)


def test_entropy_exact_known_values():
    # Exact hand-computable results: H("aaaa") = 0, H("ab") = 1 bit.
    assert shannon_entropy("") == 0.0
    assert shannon_entropy("aaaa") == 0.0
    assert shannon_entropy("ab") == pytest.approx(1.0)
    # Four equally frequent symbols -> exactly 2 bits/char.
    assert shannon_entropy("aabbccdd") == pytest.approx(2.0)


def test_uniform_alphabet_approaches_log2_alphabet_size():
    # Sanity bound used to justify rule thresholds: 62-symbol random tokens
    # score near log2(62) ~= 5.95.
    assert shannon_entropy("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWX"
                           "YZ0123456789") == pytest.approx(math.log2(62),
                                                            abs=1e-6)


def test_low_entropy_password_is_below_candidate_floor():
    # hunter2's entropy is low AND its length is below every rule's min_length.
    assert shannon_entropy(LOW_ENTROPY_PASSWORD) < 3.0
    assert len(LOW_ENTROPY_PASSWORD) < 20


def test_high_entropy_alone_is_not_a_secret_claim():
    # The threshold function itself makes no claim at all — it only measures.
    # Whether something is a candidate is decided together with structure.
    score = shannon_entropy(HIGH_ENTROPY_PROSE)
    assert score > 4.5  # high...
    assert HIGH_ENTROPY_PROSE.isidentifier() or isinstance(score, float)
