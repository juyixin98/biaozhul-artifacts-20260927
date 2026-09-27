"""文本规范层测试：非法 UTF-8 的各类原因必须被区分并精确定位。"""
from __future__ import annotations

import pytest

from app.errors import InvalidUtf8Error
from app.textnorm import (
    compute_stats,
    decode_strict,
    nfc_normalize,
    sha256_hex,
    validate_utf8,
)


# (名称, 非法字节, 期望 reason, 期望 offset)
INVALID_CASES = [
    ("截断的两字节", b"a\xc3", "truncated_sequence", 1),
    ("截断的三字节", b"a\xe2\x82", "truncated_sequence", 1),
    ("截断的四字节", b"a\xf0\x9f\x91", "truncated_sequence", 1),
    ("孤立延续字节", b"a\x80b", "unexpected_continuation", 1),
    ("非法前导0xFF", b"a\xff", "invalid_lead_byte", 1),
    ("非法前导0xFE", b"\xfe", "invalid_lead_byte", 0),
    ("过长两字节", b"\xc0\x80", "overlong_encoding", 1),
    ("过长三字节", b"\xe0\x80\x80", "overlong_encoding", 1),
    ("过长四字节", b"\xf0\x80\x80\x80", "overlong_encoding", 1),
    ("代理码点ED A0 80", b"\xed\xa0\x80", "surrogate_codepoint", 1),
    ("超范围F4 90 80 80", b"\xf4\x90\x80\x80", "codepoint_out_of_range", 1),
    ("错误延续0xC3 0x41", b"\xc3A", "invalid_continuation", 1),
]


@pytest.mark.parametrize("name,data,reason,offset", INVALID_CASES)
def test_invalid_utf8_categories(name, data, reason, offset):
    with pytest.raises(InvalidUtf8Error) as exc_info:
        validate_utf8(data)
    err = exc_info.value
    assert err.details["reason"] == reason, err.details
    assert err.details["offset"] == offset, err.details
    assert err.code == "INVALID_UTF8"


def test_strict_decode_rejects():
    with pytest.raises(InvalidUtf8Error) as ei:
        decode_strict(b"ok\xffbad")
    assert ei.value.details["offset"] == 2


@pytest.mark.parametrize(
    "data",
    [b"", b"ascii", "é".encode(), "é".encode(), "🇺🇳".encode(),
     "👨‍👩‍👧".encode(), b"a\r\nb", b"\xef\xbb\xbfa"],
)
def test_valid_utf8_roundtrip(data):
    validate_utf8(data)
    text = decode_strict(data)
    assert text.encode("utf-8") == data


def test_sha_and_stats():
    raw = "aé\r\nb".encode()
    text = decode_strict(raw)
    digest = sha256_hex(raw)
    assert digest == sha256_hex(raw)
    # 独立计算的 sha256（写死，防止实现自我引用）
    import hashlib
    assert digest == hashlib.sha256(raw).hexdigest()

    stats = compute_stats(text, raw)
    assert stats.byte_count == 6
    assert stats.codepoint_count == 5  # a é CR LF b
    assert stats.has_crlf is True
    assert stats.has_bom is False
    assert stats.nfc_identical is True


def test_bom_and_non_nfc_detected():
    raw = b"\xef\xbb\xbfe\xcc\x81"  # BOM + e + combining acute
    stats = compute_stats(decode_strict(raw), raw)
    assert stats.has_bom is True
    assert stats.nfc_identical is False
    assert nfc_normalize(decode_strict(raw)) != decode_strict(raw)


def test_all_three_byte_lengths_for_emoji():
    # U+1F466 4 字节；U+1F3FD 4 字节
    raw = "👦🏽".encode()
    assert len(raw) == 8
    validate_utf8(raw)
