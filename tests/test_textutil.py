"""文本规范与原始字节范围测试（多字节）。"""
from __future__ import annotations

import pytest

from app.errors import TextNotUnicodeError
from app.textutil import ByteOffsetMap, make_spec, normalize_text

from . import fixtures


def test_normalize_rejects_invalid_utf8(record):
    bad = fixtures.invalid_utf8_bytes()
    with pytest.raises(TextNotUnicodeError) as ei:
        normalize_text(bad)
    record.fail_category(ei.value.code, ei.value.category, ei.value.http_status)
    assert ei.value.code == "INPUT_TEXT_NOT_UNICODE"
    assert isinstance(ei.value.details["byte"], int)


def test_spec_stable(record):
    t = fixtures.multibyte_text()
    s1, s2 = make_spec(t), make_spec(t)
    assert s1 == s2
    assert s1.byte_len > s1.char_len  # 含多字节字符
    record.state("spec", s1.__dict__, "字节长度应严格大于码点长度（含中文/希腊字母）")


def test_multibyte_byte_spans(record):
    t = "a你b好c"
    bm = ByteOffsetMap(t, block=2)
    # 码点: 0 a,1 你,2 b,3 好,4 c ; 字节: a=0,你=1..3,b=4,好=5..7,c=8
    cases = [
        (0, 1, (0, 1)),
        (1, 2, (1, 4)),
        (3, 4, (5, 8)),
        (0, 5, (0, 9)),
        (2, 3, (4, 5)),
    ]
    for cs, ce, expected in cases:
        got = bm.verify_byte_span(cs, ce)
        record.check(f"byte span [{cs},{ce})", ok=got == expected, expected=expected, actual=got)
        assert got == expected


def test_byte_span_offsets_on_real_fixture(record):
    t = fixtures.multibyte_text()
    bm = ByteOffsetMap(t, block=4)
    # “你好” 起始码点索引与其字节索引应不同且往返一致
    idx = t.index("你好")
    b0, b1 = bm.verify_byte_span(idx, idx + 2)
    encoded = t.encode("utf-8")
    record.state("你好", {"char": [idx, idx + 2], "byte": [b0, b1]})
    assert encoded[b0:b1].decode("utf-8") == "你好"
    assert b0 > idx  # 前面有多字节字符


def test_large_map_consistent_with_naive(record):
    t = fixtures.large_multibyte_text(50_000)
    bm = ByteOffsetMap(t, block=1024)
    # 朴素算法做抽样对照
    sample_points = [0, 1, 99, 1024, 1025, 12_345, 33_333, len(t)]
    encoded = t.encode("utf-8")
    for p in sample_points:
        naive = len(t[:p].encode("utf-8"))
        fast = bm.char_to_byte(p)
        record.check(f"offset@{p}", ok=fast == naive, expected=naive, actual=fast)
        assert fast == naive
    assert bm.total_bytes == len(encoded)


def test_ascii_fast_path_identity():
    bm = ByteOffsetMap("plain ascii text")
    assert bm.char_to_byte(7) == 7
    assert bm.verify_byte_span(0, 16) == (0, 16)
