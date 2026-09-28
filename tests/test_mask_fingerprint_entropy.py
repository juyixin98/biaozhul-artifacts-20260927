"""Masking, fingerprint and entropy unit tests (independent expected vectors)."""

from __future__ import annotations

import hmac
import hashlib
import math
from collections import Counter

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from secretscan.entropy import iter_token_spans, shannon_entropy
from secretscan.fingerprint import CandidateFingerprinter, file_sha256
from secretscan.mask import mask_value

# --------------------------------------------------------------------- masks
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", "…"),
        ("a", "a…"),
        ("abcd", "a…"),
        ("abcde", "ab…e"),
        ("abcdefg", "ab…g"),
        ("abcdefgh", "abcd…gh"),
        ("AKIAFAKE000000000001", "AKIA…01"),
        ("ghp_0123456789abcdefghijklmnopqrstuvwxyz", "ghp_…yz"),
    ],
)
def test_mask_shapes(raw, expected):
    assert mask_value(raw) == expected


def test_mask_never_contains_more_than_six_original_chars():
    raw = "AKIAFAKE000000000001"
    masked = mask_value(raw)
    # At most prefix(4) + suffix(2) original characters survive.
    survivors = sum(ch in raw for ch in masked if ch.isalnum())
    assert survivors <= 6


# ------------------------------------------------------------- fingerprints
def _independent_hmac(master: bytes, salt: bytes, raw: bytes) -> str:
    """Reference derivation written independently of secretscan.fingerprint."""
    key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=salt,
        info=b"secretscan-candidate-fingerprint-v1",
    ).derive(master)
    return hmac.new(key, raw, hashlib.sha256).hexdigest()


def test_fingerprint_matches_independent_reference(fixed_keys):
    master, salt = fixed_keys
    fp = CandidateFingerprinter(master, salt)
    raw = b"AKIAFAKE000000000001"
    expected = "eeb918c321f09900aeb1f58f199b0ec44c3da8e78de0049382e885c324bd6cba"
    assert fp.fingerprint(raw) == expected
    assert fp.fingerprint(raw) == _independent_hmac(master, salt, raw)


def test_fingerprint_depends_on_project_salt(fixed_keys):
    master, salt = fixed_keys
    other_salt = bytes([0x33]) * 16
    a = CandidateFingerprinter(master, salt).fingerprint(b"same-value")
    b = CandidateFingerprinter(master, other_salt).fingerprint(b"same-value")
    assert a != b  # project isolation: same content, different project


def test_file_sha256_known_vector():
    assert file_sha256(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


# ------------------------------------------------------------------ entropy
def _ref_entropy(s: bytes) -> float:
    c = Counter(s)
    n = len(s)
    return -sum(v / n * math.log2(v / n) for v in c.values())


def test_entropy_matches_reference_formula():
    s = b"Xq7vKp2mZbW8xNdR4tYcL6sHgFjAeQ3uVwBnCaQ"
    assert shannon_entropy(s) == pytest.approx(_ref_entropy(s), abs=1e-12)
    assert shannon_entropy(s) > 4.5


def test_repeating_hex_digest_is_below_threshold():
    # Ordinary structured text: 64 hex chars but Shannon entropy 4.0.
    s = b"0123456789abcdef" * 4
    assert shannon_entropy(s) == pytest.approx(4.0)
    assert not any(
        shannon_entropy(tok) >= 4.5
        for _, _, tok in iter_token_spans(s, 24)
    )


def test_tokenizer_skips_short_and_non_ascii_runs():
    content = b"abc \x00\x01\x02 ghp_0123456789abcdefghijklmnopqrstuvwxyz tail"
    spans = list(iter_token_spans(content, 24))
    tokens = [t for _, _, t in spans]
    assert b"abc" not in tokens
    assert b"ghp_0123456789abcdefghijklmnopqrstuvwxyz" in tokens
