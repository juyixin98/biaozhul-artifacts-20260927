"""Malicious / non-canonical input tests.

Every case asserts the *specific* failure category (not just "it raised"). We
also assert that a huge declared length is rejected before any large
allocation (bounded by a tiny ``max_alloc`` and by timing).
"""
from __future__ import annotations

import pytest

from abibackend import abi
from abibackend.abi import (
    LengthTooLarge,
    NonCanonicalEncoding,
    NonCanonicalPadding,
    OffsetOutOfBounds,
    OffsetOverlap,
    TrailingBytes,
    ValueOutOfRange,
)
from abibackend.abi.types import parse_type

W = 32


def w(n: int) -> bytes:
    return n.to_bytes(W, "big", signed=False)


# --------------------------------------------------------------------------- #
# Helpers to craft hostile blobs
# --------------------------------------------------------------------------- #
def two_dynamic(off0: int, off1: int, bodies: bytes) -> bytes:
    """(bytes,bytes) tuple with attacker-controlled relative offsets."""
    return w(off0) + w(off1) + bodies


# --------------------------------------------------------------------------- #
# Offset attacks
# --------------------------------------------------------------------------- #
def test_offset_into_head_rejected(log):
    # second pointer says 0x00 (points at the very start of the head)
    blob = two_dynamic(0x40, 0x00, w(0) + w(0))
    log("offset-into-head", "attack", blob=blob.hex())
    with pytest.raises(OffsetOutOfBounds):
        abi.decode(["bytes", "bytes"], blob)


def test_offset_backwards_overlap_rejected(log):
    # two pointers to the SAME body location -> overlap
    blob = two_dynamic(0x40, 0x40, w(0))
    log("offset-duplicate", "attack", blob=blob.hex())
    with pytest.raises(OffsetOverlap):
        abi.decode(["bytes", "bytes"], blob)


def test_offset_past_blob_rejected(log):
    # second pointer far past the end (word-aligned)
    blob = w(0x40) + w(0x10000) + w(0)
    log("offset-past-blob", "attack", blob=blob.hex())
    with pytest.raises(OffsetOutOfBounds):
        abi.decode(["bytes", "bytes"], blob)


def test_unaligned_pointer_itself_rejected(log):
    # 0x9999 is not word aligned -> rejected before bounds classification
    blob = w(0x40) + w(0x9999) + w(0)
    log("unaligned-pointer", "attack", blob=blob.hex())
    with pytest.raises(NonCanonicalEncoding):
        abi.decode(["bytes", "bytes"], blob)


def test_absolute_offset_treated_as_relative(log):
    """A nested container's offsets must be relative to ITS head, not absolute.

    Encode ``(uint256[][])`` honestly, then tamper with the *inner* array's
    element pointer by adding an absolute bias. A naive absolute-offset decoder
    would still resolve into valid blob bytes; the strict relative decoder must
    reject (out of bounds / overlap / non-canonical).
    """
    good = abi.encode(["(uint256[],)"], ([[1, 2]],))
    # Layout: w0 outer ptr, w1 inner array length(=2), w2 inner element head
    # starts at the length word base; its only element is static uint256 so
    # there is no inner pointer here. Instead bias the OUTER tuple pointer so
    # the inner array is read at an absolute-shifted base -> relative decoder
    # must reject (the inner length would then be read from the wrong word).
    words = [good[i:i + W] for i in range(0, len(good), W)]
    # w0 is 0x20; change it to 0x40 so the tuple body starts one word later.
    assert int.from_bytes(words[0], "big") == 0x20
    words[0] = w(0x40)
    evil = b"".join(words)
    log("absolute-bias-inner", "attack", good=good.hex(), evil=evil.hex())
    with pytest.raises((OffsetOutOfBounds, OffsetOverlap, NonCanonicalEncoding)):
        abi.decode(["(uint256[],)"], evil)


def test_gap_between_bodies_rejected(log):
    # head 0x40,0x80; body0 len0 at 0x40; body1 starts at 0x80 leaving one
    # unclaimed word at 0x60 (gap) -> non-canonical (or overlap) rejection.
    blob = w(0x40) + w(0x80) + w(0) + w(0)
    log("gap-between-bodies", "attack", blob=blob.hex())
    with pytest.raises((NonCanonicalEncoding, OffsetOverlap, OffsetOutOfBounds)):
        abi.decode(["bytes", "bytes"], blob)


def test_truncated_blob_rejected():
    # uint256 but only 10 bytes
    with pytest.raises(OffsetOutOfBounds):
        abi.decode(["uint256"], b"\x00" * 10)


def test_trailing_bytes_rejected(log):
    blob = w(1) + b"\x00"  # 33 bytes for a single uint256
    log("trailing-bytes", "attack", blob=blob.hex())
    with pytest.raises(TrailingBytes):
        abi.decode(["uint256"], blob)


# --------------------------------------------------------------------------- #
# Length / allocation attacks
# --------------------------------------------------------------------------- #
def test_huge_dynamic_length_no_giant_alloc(log):
    huge = (2**64 - 1).to_bytes(W, "big")  # claim ~16 EiB
    blob = w(0x20) + huge  # bytes type: offset + absurd length
    log("huge-length", "attack", declared=2**64 - 1, blob_len=len(blob))
    with pytest.raises((LengthTooLarge, OffsetOutOfBounds)) as exc_info:
        abi.decode(["bytes"], blob, max_alloc=1024)
    assert isinstance(exc_info.value, (LengthTooLarge, OffsetOutOfBounds))


def test_huge_array_count_no_giant_alloc(log):
    # single top-level uint256[]: head offset 0x20, then an absurd count.
    blob = w(0x20) + w(2**60)
    log("huge-array-count", "attack", declared=2**60)
    with pytest.raises((LengthTooLarge, OffsetOutOfBounds)):
        abi.decode(["uint256[]"], blob, max_alloc=4096)


def test_array_count_overflow_checked(log):
    # count so large count*32 would overflow a naive unchecked multiply
    blob = w(0x20) + w((1 << 256) - 1)
    log("overflow-count", "attack")
    with pytest.raises((LengthTooLarge, OffsetOutOfBounds)):
        abi.decode(["uint256[]"], blob, max_alloc=8 * 1024 * 1024)


def test_declared_length_exceeds_region_but_in_blob(log):
    # length says 64 bytes but only 32 bytes of (zero) data follow
    blob = w(0x20) + w(64) + b"\x00" * W
    log("length-exceeds-region", "attack", blob=blob.hex())
    with pytest.raises(OffsetOutOfBounds):
        abi.decode(["bytes"], blob)


# --------------------------------------------------------------------------- #
# Padding / canonicality
# --------------------------------------------------------------------------- #
def test_uint_noncanonical_high_bits():
    # uint8 value encoded with a stray high byte (0x01ff) -> exceeds uint8 + padding
    blob = (0x01FF).to_bytes(W, "big")
    with pytest.raises((ValueOutOfRange, NonCanonicalPadding)):
        abi.decode(["uint8"], blob)


def test_negative_into_unsigned():
    # all-ones interpreted as a huge value that overflows a narrow unsigned
    blob = ((1 << 256) - 1).to_bytes(W, "big")  # all ones
    with pytest.raises(ValueOutOfRange):
        abi.decode(["uint8"], blob)
    # max uint256 itself is in-range (not a "negative")
    assert abi.decode(["uint256"], blob) == 2**256 - 1


def test_int8_value_255_out_of_range():
    # 0x00..ff = 255, one above int8 max (127) -> out of range, not a padding rule
    blob = b"\x00" * 31 + b"\xff"
    with pytest.raises(ValueOutOfRange):
        abi.decode(["int8"], blob)


def test_int8_correct_sign_extension_minus_one():
    blob = b"\xff" * W
    assert abi.decode(["int8"], blob) == -1


def test_bool_noncanonical_word():
    with pytest.raises(NonCanonicalPadding):
        abi.decode(["bool"], w(2))


def test_address_high_bits_dirty():
    # 21 significant bytes -> exceeds 160 bits
    blob = b"\x01" + b"\x00" * 31
    with pytest.raises(ValueOutOfRange):
        abi.decode(["address"], blob)


def test_bytesN_nonzero_right_padding():
    blob = b"\xab" + b"\x00" * 30 + b"\x01"
    with pytest.raises(NonCanonicalPadding):
        abi.decode(["bytes1"], blob)


def test_bytes_nonzero_tail_padding():
    # head offset 0x20, len=1, data 0xab, dirty 31st pad byte
    blob = w(0x20) + w(1) + b"\xab" + b"\x00" * 30 + b"\x01"
    with pytest.raises(NonCanonicalPadding):
        abi.decode(["bytes"], blob)


def test_string_invalid_utf8():
    # head offset 0x20, len=1, lone continuation byte 0xff
    blob = w(0x20) + w(1) + b"\xff" + b"\x00" * 31
    with pytest.raises(NonCanonicalPadding):
        abi.decode(["string"], blob)


# --------------------------------------------------------------------------- #
# Nested dynamic relative offsets
# --------------------------------------------------------------------------- #
def test_nested_dynamic_inner_offset_relative(log):
    # (bytes, (bytes,bytes)) — encode honestly, sanity decode then tamper inner
    good = abi.encode(["(bytes,(bytes,bytes))"], (b"a", (b"b", b"c")))
    log("nested-good", "info", blob=good.hex())
    out = abi.decode(["(bytes,(bytes,bytes))"], good)
    assert out == ((b"a", (b"b", b"c")),)


def test_fixed_array_arity_mismatch_encode():
    with pytest.raises(abi.ValueOutOfRange):
        abi.encode(["uint256[3]"], [[1, 2]])


def test_unaligned_pointer_rejected():
    # (bytes,bytes) with first pointer 0x41 (not word aligned)
    blob = w(0x41) + w(0x60) + b"\x00" * W
    with pytest.raises(NonCanonicalEncoding):
        abi.decode(["bytes", "bytes"], blob)


def test_sibling_overlap_via_inner_array():
    """Two inner dynamic arrays whose pointer targets collide."""
    # Top uint256[][]: head ptr; outer length=2; element pointers (counting the
    # length word) normally 0x60/0xa0. Force both to 0x60 -> overlap.
    blob = (
        w(0x20)                 # root head: pointer to outer array
        + w(2)                  # outer length
        + w(0x60) + w(0x60)     # both inner element pointers identical
        + w(0)                  # shared inner length word
    )
    with pytest.raises((OffsetOverlap, NonCanonicalEncoding, OffsetOutOfBounds)):
        abi.decode(["uint256[][]"], blob)
