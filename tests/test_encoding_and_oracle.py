"""Encoding tests, including cross-validation against the independent oracle.

The oracle does NOT import the core; comparing its roots/commitments to the
core's on the same parsed objects catches silent divergence between the
reference and the implementation.
"""

import pytest

from lc import encoding
from lc.types import Checkpoint, Committee, Header


def test_header_codec_roundtrip():
    h = Header(
        round=42,
        parent_root=b"p" * 32,
        body_root=b"b" * 32,
        timestamp_ms=123456789,
        next_committee_commitment=b"c" * 32,
    )
    raw = encoding.encode_header(h)
    assert len(raw) == encoding.HEADER_FIXED_SIZE
    h2 = encoding.decode_header(raw)
    assert h2 == h


def test_header_codec_without_rotation_roundtrips():
    h = Header(7, b"\x00" * 32, b"x" * 32, 9, None)
    assert encoding.decode_header(encoding.encode_header(h)) == h


def test_decode_rejects_wrong_length():
    with pytest.raises(ValueError):
        encoding.decode_header(b"\x00" * 10)


def test_decode_rejects_bad_presence_flag():
    h = Header(1, b"\x00" * 32, b"x" * 32, 1, None)
    raw = bytearray(encoding.encode_header(h))
    raw[8 + 32 + 32 + 8] = 7  # invalid presence flag
    with pytest.raises(ValueError):
        encoding.decode_header(bytes(raw))


def test_committee_codec_roundtrip_and_canonical_order():
    parsed = Checkpoint.from_dict(
        {"header": _minimal_header_dict(), "committee": _committee_dict()},
        committee_max_size=256,
    ).committee
    raw = encoding.encode_committee(parsed)
    decoded = encoding.decode_committee(raw)
    assert decoded == parsed
    # canonical ascending key order
    keys = [m.public_key for m in decoded.members]
    assert keys == sorted(keys)


def test_oracle_and_core_agree_on_checkpoint_root(golden):
    cp = golden["checkpoint"]
    header = Header.from_dict(cp["header"])
    committee = Committee.from_dict(cp["committee"], max_size=256)
    assert "0x" + encoding.header_root(header).hex() == cp["root"]
    assert (
        "0x" + encoding.committee_commitment(committee).hex()
        == cp["committee_commitment"]
    )


def test_oracle_and_core_agree_on_every_vector_root(golden):
    for vec in golden["vectors"]:
        if vec["kind"] == "single":
            h = Header.from_dict(vec["header"])
            assert "0x" + encoding.header_root(h).hex() == vec["root"], vec["id"]
        else:
            for i, item in enumerate(vec["items"]):
                h = Header.from_dict(item["header"])
                assert "0x" + encoding.header_root(h).hex() == item["root"], (
                    f"{vec['id']}[{i}]"
                )


def test_domain_separation_no_cross_hash():
    h = Header(1, b"\x00" * 32, b"x" * 32, 1, None)
    # a header hash must not equal a bare sha256 of its encoding
    import hashlib

    assert encoding.header_root(h) != hashlib.sha256(
        encoding.encode_header(h)
    ).digest()


def test_certificate_message_shape():
    root = b"r" * 32
    msg = encoding.certificate_message(root)
    assert len(msg) == 40
    assert msg[:8] == encoding.DOM_CERT_SIGN
    assert msg[8:] == root


def _minimal_header_dict():
    return {
        "round": 0,
        "parent_root": "0x" + "00" * 32,
        "body_root": "0x" + "11" * 32,
        "timestamp": 1_700_000_000_000,
        "next_committee_commitment": None,
    }


def _committee_dict():
    return {
        "members": [
            {"public_key": "0x" + "02" * 32, "weight": 10},
            {"public_key": "0x" + "01" * 32, "weight": 20},
        ]
    }
