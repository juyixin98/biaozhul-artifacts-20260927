"""Regression tests for boundary inputs that previously 'looked fine' but
silently truncated: hashes/pubkeys with leading zero nibbles."""

import pytest

from lc.errors import Category, LightClientError
from lc.types import Certificate, Header


def test_root_with_leading_zero_byte_parses_to_32_bytes(golden):
    # The golden checkpoint root is exactly such a value: 0x006c...
    root_hex = golden["checkpoint"]["root"]
    assert root_hex[2:4] == "00"
    header = Header.from_dict(golden["checkpoint"]["header"])
    from lc import encoding

    root = encoding.header_root(header)
    assert len(root) == 32
    assert root[0] == 0
    assert "0x" + root.hex() == root_hex


def test_short_hex_is_rejected_not_padded():
    # one nibble short — must be INPUT, never silently zero-padded
    bad = {
        "round": 1,
        "parent_root": "0x" + "ab" * 31,  # 31 bytes
        "body_root": "0x" + "11" * 32,
        "timestamp": 1,
        "next_committee_commitment": None,
    }
    with pytest.raises(LightClientError) as ei:
        Header.from_dict(bad)
    assert ei.value.category is Category.INPUT
    assert ei.value.details["expected_digits"] == 64
    assert ei.value.details["got_digits"] == 62


def test_odd_length_hex_is_rejected():
    bad = {
        "round": 1,
        "parent_root": "0xabc",
        "body_root": "0x" + "11" * 32,
        "timestamp": 1,
        "next_committee_commitment": None,
    }
    with pytest.raises(LightClientError) as ei:
        Header.from_dict(bad)
    assert ei.value.category is Category.INPUT


def test_certificate_leading_zero_root_must_match(golden):
    # A cert binding to the 0x00.. checkpoint root must equal the parsed root;
    # if either side truncated a nibble the binding would spuriously mismatch.
    cp = golden["checkpoint"]
    cert = Certificate.from_dict(cp["certificate"])
    header = Header.from_dict(cp["header"])
    from lc import encoding

    assert cert.header_root == encoding.header_root(header)
    assert len(cert.header_root) == 32
