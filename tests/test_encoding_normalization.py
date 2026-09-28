"""Tests for strict text validation / normalization (text canonicalization).

Each test asserts a concrete result or a concrete failure *category+code*.
"""

from __future__ import annotations

import json

import pytest

from textindex import encoding, normalizer
from textindex.errors import (
    CATEGORY_INPUT_ERROR,
    InvalidUtf8,
    UnpairedSurrogate,
    UnsupportedNormalization,
)

from . import fixtures


def test_decode_valid_ascii_and_multibyte():
    assert encoding.decode_utf8(b"abc") == "abc"
    assert encoding.decode_utf8("é".encode()) == "é"
    assert encoding.decode_utf8("\U0001F600".encode()) == "\U0001F600"


@pytest.mark.parametrize("name,raw", fixtures.INVALID_UTF8.items())
def test_invalid_utf8_rejected_with_byte_span(name, raw, recorder):
    rec, counts = recorder
    try:
        encoding.decode_utf8(raw)
    except InvalidUtf8 as exc:
        passed = exc.category == CATEGORY_INPUT_ERROR
        counts["PASS" if passed else "FAIL"] += 1
        rec.judge(
            test=f"invalid_utf8[{name}]", kind="negative", passed=passed,
            expected="InvalidUtf8/input_error",
            actual=f"{exc.code}/{exc.category}",
            intermediate={"raw_hex": raw.hex(),
                          "start_byte": exc.details["start_byte"],
                          "end_byte": exc.details["end_byte"]},
            reason="strict decoder must reject with byte offsets",
            error_category=exc.category, error_code=exc.code,
        )
        assert exc.details["start_byte"] < len(raw)
        assert exc.details["end_byte"] <= len(raw)
    else:
        counts["FAIL"] += 1
        rec.judge(test=f"invalid_utf8[{name}]", kind="negative",
                  passed=False, expected="InvalidUtf8", actual="accepted",
                  reason="malformed UTF-8 was accepted")
        pytest.fail(f"{name} was accepted: {raw.hex()}")


def test_lone_surrogate_in_json_string_rejected(recorder):
    rec, counts = recorder
    text = json.loads(fixtures.JSON_LONE_SURROGATE)  # parser accepts escape
    assert ord(text) == 0xD800
    with pytest.raises(UnpairedSurrogate) as ei:
        encoding.ensure_scalar_value(text)
    passed = ei.value.details["codepoint"] == "U+D800"
    counts["PASS" if passed else "FAIL"] += 1
    rec.judge(test="json_lone_surrogate", kind="negative", passed=passed,
              expected="U+D800", actual=ei.value.details["codepoint"],
              reason="JSON \\ud800 escape must not reach the index")
    with pytest.raises(UnpairedSurrogate):
        encoding.encode_utf8(text)


def test_lead_boundary_predicate():
    data = "aé\U0001F600".encode()
    # a=0, é starts 1 (2 bytes), 😀 starts 3 (4 bytes), end 7
    assert [i for i in range(len(data) + 1)
            if encoding.is_lead_boundary(data, i)] == [0, 1, 3, 7]
    assert not encoding.is_lead_boundary(data, 2)
    assert not encoding.is_lead_boundary(data, 4)
    assert encoding.is_lead_boundary(data, 7)
    assert not encoding.is_lead_boundary(data, 8)


def test_normalization_nfc_composes_decomposed(recorder):
    rec, counts = recorder
    out = normalizer.canonicalize("é", "NFC")
    passed = out == "é"
    counts["PASS" if passed else "FAIL"] += 1
    rec.judge(test="nfc_composes", kind="canonical", passed=passed,
              expected="é(U+00E9)", actual=out,
              intermediate={"codepoints": [f"U+{ord(c):04X}" for c in out]},
              reason="NFC must compose e + combining acute")


def test_normalization_none_preserves_bytes():
    out = normalizer.canonicalize("é", "NONE")
    assert out == "é" and len(out) == 2


def test_unsupported_normalization_is_input_error():
    with pytest.raises(UnsupportedNormalization) as ei:
        normalizer.canonicalize("a", "NFKC_CF")
    assert ei.value.category == CATEGORY_INPUT_ERROR
    assert "NFKC" in ei.value.details["supported"]
