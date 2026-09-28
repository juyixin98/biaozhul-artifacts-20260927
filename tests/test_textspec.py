"""Tests for text normalization, UTF-8 byte maps and byte-range validation."""

from __future__ import annotations

import pytest

from app.errors import InvalidByteRangeError, TextDecodeError
from app.textspec import (
    ByteIndex,
    normalize_source,
    sha256_hex,
    validate_byte_range,
)


def test_normalize_accepts_str_and_identical_bytes():
    nt = normalize_source("abc")
    assert nt.data == b"abc"
    assert nt.size == 3
    assert nt.as_str() == "abc"


def test_normalize_rejects_invalid_utf8_with_code(run_logger, request):
    bad = b"a\xffb"  # 0xFF is never a valid UTF-8 leading byte
    with pytest.raises(TextDecodeError) as exc:
        normalize_source(bad)
    assert exc.value.code == "text_not_utf8"
    assert exc.value.details["start"] == 1
    run_logger.check(
        request.node.nodeid,
        "invalid-utf8 classified",
        expected="text_not_utf8",
        actual=exc.value.code,
        passed=exc.value.code == "text_not_utf8",
        reason="0xFF can never start a UTF-8 sequence",
        intermediate={"input_hex": bad.hex(), "details": exc.value.details},
    )


def test_newline_normalization_is_opt_in_and_recorded():
    raw = "a\r\nb\rc"
    assert normalize_source(raw).data == b"a\r\nb\rc"
    nt = normalize_source(raw, normalize_newlines=True)
    assert nt.data == b"a\nb\nc"
    assert nt.normalize_newlines is True


def test_bom_is_preserved():
    nt = normalize_source("﻿hello")
    assert nt.data.startswith(b"\xef\xbb\xbf")


def test_byte_index_multibyte_boundaries_and_counts():
    # a(1) €(3) b(1) space(1) 世(3) 界(3)
    data = "a€b 世界".encode()
    idx = ByteIndex(data)
    # codepoints: a € b sp 世 界 = 6
    assert idx.codepoints == 6
    # starts must be [0, 1, 4, 5, 6, 9, 12]
    assert list(idx.starts) == [0, 1, 4, 5, 6, 9, 12]
    # advancing from inside € (offset 2) jumps to its end boundary 4
    assert idx.advance(2) == 4
    assert idx.advance(len(data)) is None


def test_range_validation_accepts_whole_codepoints(run_logger, request):
    data = "a€b".encode()  # 0:a 1-3:€ 4:b ; len 5
    validate_byte_range(data, 0, 5)
    validate_byte_range(data, 1, 4)       # exactly €
    validate_byte_range(data, 0, 0)       # zero-width at start
    validate_byte_range(data, 4, 4)       # zero-width at b start
    run_logger.check(
        request.node.nodeid,
        "whole-codepoint ranges valid",
        expected="no raise",
        actual="no raise",
        passed=True,
        reason="1:4 covers exactly the 3 bytes of € and aligns both ends",
        intermediate={"starts": list(ByteIndex(data).starts)},
    )


def test_range_validation_rejects_mid_codepoint():
    data = "a€b".encode()
    # start/end mid-codepoint, or an end that stops inside €; (1,5)=€b is
    # legal and deliberately NOT listed.
    for s, e in ((2, 3), (1, 2), (2, 2), (1, 3), (0, 2)):
        with pytest.raises(InvalidByteRangeError) as exc:
            validate_byte_range(data, s, e)
        assert exc.value.code == "invalid_byte_range"


def test_range_validation_rejects_oor_and_order():
    data = b"abc"
    for s, e in ((0, 4), (-1, 1), (2, 1)):
        with pytest.raises(InvalidByteRangeError):
            validate_byte_range(data, s, e)


def test_sha_is_of_raw_bytes():
    import hashlib

    data = "a€b".encode()
    assert sha256_hex(data) == hashlib.sha256(data).hexdigest()
