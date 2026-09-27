"""RTP 解析/构造的具体错误类别断言（不是“接口能调用”）。"""

from __future__ import annotations

import pytest

from app.media.rtp import build_rtp, parse_rtp, RtpParseError


def test_build_then_parse_roundtrip() -> None:
    payload = bytes(range(64))
    raw = build_rtp(sequence=0xABCD, timestamp=0x12345678,
                    ssrc=0xDEADBEEF, payload=payload, marker=True,
                    payload_type=8)
    pkt = parse_rtp(raw)
    assert pkt.version == 2
    assert pkt.sequence == 0xABCD
    assert pkt.timestamp == 0x12345678
    assert pkt.ssrc == 0xDEADBEEF
    assert pkt.marker is True
    assert pkt.payload_type == 8
    assert pkt.payload == payload


def test_csrc_and_extension_are_skipped_to_payload() -> None:
    # 手工构造：V=2, X=1, CC=2, PT=0, seq=1, ts=160, ssrc=0x11223344
    out = bytearray()
    out += bytes([0x92, 0x00]) + (1).to_bytes(2, "big")
    out += (160).to_bytes(4, "big")
    out += (0x11223344).to_bytes(4, "big")
    out += (0x55667788).to_bytes(4, "big")  # CSRC 1
    out += (0x99AABBCC).to_bytes(4, "big")  # CSRC 2
    out += (0xBEEF).to_bytes(2, "big")      # extension profile/id（2 字节）
    out += (1).to_bytes(2, "big")           # 扩展长度 1 word（2 字节）
    assert len(out) == 12 + 8 + 4
    out += b"EXTD"                          # 4 字节扩展数据
    out += b"payload"
    pkt = parse_rtp(bytes(out))
    assert pkt.csrc == (0x55667788, 0x99AABBCC)
    assert pkt.extension is True
    assert pkt.payload == b"payload"


def test_bad_extension_length() -> None:
    out = bytearray()
    out += bytes([0x92, 0x00]) + (1).to_bytes(2, "big")  # X=1, CC=2
    out += (160).to_bytes(4, "big") + (1).to_bytes(4, "big")
    out += (0x55).to_bytes(4, "big") + (0x66).to_bytes(4, "big")
    out += (0xBEEF).to_bytes(2, "big") + (10).to_bytes(2, "big")  # profile+长度 10
    # 报文到此结束（24 字节），但扩展声称 10 word，数据缺失
    with pytest.raises(RtpParseError) as exc:
        parse_rtp(bytes(out))
    assert exc.value.reason == "bad_extension"


def test_truncated_fixed_header() -> None:
    with pytest.raises(RtpParseError) as exc:
        parse_rtp(b"\x80\x60")
    assert exc.value.reason == "truncated"


def test_bad_version() -> None:
    raw = bytearray(build_rtp(sequence=1, timestamp=160, ssrc=1, payload=b"x"))
    raw[0] = 0x40  # version=1
    with pytest.raises(RtpParseError) as exc:
        parse_rtp(bytes(raw))
    assert exc.value.reason == "bad_version"


def test_payload_type_masked_separately_from_marker() -> None:
    # marker=1 与 PT=96（动态范围）必须分别解析，互不污染
    raw = build_rtp(sequence=1, timestamp=160, ssrc=1, payload=b"x",
                    marker=True, payload_type=96)
    pkt = parse_rtp(raw)
    assert pkt.marker is True
    assert pkt.payload_type == 96
    # marker=0, PT=127（7 位字段可表达的最大值）合法
    raw2 = build_rtp(sequence=1, timestamp=160, ssrc=1, payload=b"x",
                     payload_type=127)
    assert parse_rtp(raw2).payload_type == 127


def test_bad_padding_count() -> None:
    # P 位置 1，但负载只有 1 字节，末字节声称填充 100
    head = bytearray(12)
    head[0] = 0xA0
    head[1] = 0x00
    raw = bytes(head) + b"\x64"
    with pytest.raises(RtpParseError) as exc:
        parse_rtp(raw)
    assert exc.value.reason == "bad_padding"


def test_valid_padding_is_stripped() -> None:
    raw = build_rtp(sequence=1, timestamp=160, ssrc=1,
                    payload=b"abc", padding=4)
    pkt = parse_rtp(raw)
    assert pkt.payload == b"abc"
    assert pkt.padding is True
