"""Unit tests for spec normalization and opaque cursors."""
from __future__ import annotations

import pytest

from app.errors import (
    EncodingError,
    InvalidBase64Error,
    InvalidCursorError,
    UnsupportedEncodingError,
)
from app.pagination import PageCursor
from app.spec import (
    SUPPORTED_ENCODINGS, CaseMode, TextSpec, ascii_casefold,
    decode_base64, encode_base64,
)


# ---- base64 transport --------------------------------------------------------

def test_base64_roundtrip():
    for b in [b"", b"\x00", b"\xff\xfe", b"abc", bytes(range(256))]:
        assert decode_base64(encode_base64(b)) == b


@pytest.mark.parametrize("bad", ["a", "@@", "YW Jj", "YWJj=", "YWJj==",
                                 "???", "abc"])
def test_base64_rejects_slop(bad):
    with pytest.raises(InvalidBase64Error):
        decode_base64(bad)


def test_canonical_padding_required():
    # "f" -> "Zg==" ; truncated forms must be rejected.
    with pytest.raises(InvalidBase64Error):
        decode_base64("Zg")
    with pytest.raises(InvalidBase64Error):
        decode_base64("Zg=")
    assert decode_base64("Zg==") == b"f"


# ---- encodings ---------------------------------------------------------------

def test_unsupported_encoding_refused():
    with pytest.raises(UnsupportedEncodingError):
        TextSpec(encoding="utf-7")
    assert "binary" in SUPPORTED_ENCODINGS


def test_invalid_utf8_pattern_typed_error():
    spec = TextSpec(encoding="utf-8")
    with pytest.raises(EncodingError) as ei:
        spec.normalize_pattern(b"\xff")
    assert ei.value.details["bad_offset"] == 0


def test_binary_spec_accepts_anything():
    spec = TextSpec(encoding="binary")
    assert spec.normalize_pattern(bytes(range(256))) == bytes(range(256))


def test_ascii_casefold_is_bytewise_and_preserves_length():
    spec = TextSpec(encoding="binary",
                    case_mode=CaseMode.ASCII_CASEFOLD)
    raw = b"ABC\xff\x00xYz"
    out = spec.normalize_pattern(raw)
    assert out == b"abc\xff\x00xyz"
    assert len(out) == len(raw)


def test_ascii_casefold_helper():
    assert ascii_casefold(b"AbZ\x00") == b"abz\x00"


# ---- cursor tampering/staleness ---------------------------------------------

def test_cursor_roundtrip():
    c = PageCursor(scan_id="s1", epoch=3, after_seq=99)
    tok = c.to_token("secret")
    again = PageCursor.from_token(tok, "secret")
    assert again == c


def test_cursor_signature_mismatch_wrong_secret():
    tok = PageCursor("s", 1, 0).to_token("secret-a")
    with pytest.raises(InvalidCursorError):
        PageCursor.from_token(tok, "secret-b")


def test_cursor_garbage_rejected():
    for bad in ["", "nodot", "a.b.c", "....", None]:
        if bad is None:
            continue
        with pytest.raises(InvalidCursorError):
            PageCursor.from_token(bad, "secret")
