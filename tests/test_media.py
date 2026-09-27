"""RTP parser contract: valid fields decode, malformed input gets reason codes."""
import struct

import numpy as np
import pytest

from app.media import build_rtp, parse_rtp
from app.media import RtpParseError


def test_parse_roundtrip_basic():
    payload = bytes([128, 200, 60])
    raw = build_rtp(seq=12345, timestamp=99999, ssrc=0xCAFEBABE,
                    payload=payload, payload_type=0)
    pkt = parse_rtp(raw, arrival_ms=42.5)
    assert pkt.seq == 12345
    assert pkt.timestamp == 99999
    assert pkt.ssrc == 0xCAFEBABE
    assert pkt.payload == payload
    assert pkt.arrival_ms == 42.5
    assert pkt.marker is False


def test_parse_marker_and_csrc():
    b0 = 0x80 | 2  # v2 + 2 CSRC
    b1 = 0x80       # marker
    header = struct.pack("!BBHII", b0, b1, 7, 8, 9)
    csrc = struct.pack("!II", 111, 222)
    pkt = parse_rtp(header + csrc + b"abc", 0)
    assert pkt.marker is True
    assert pkt.payload == b"abc"
    assert pkt.seq == 7


def test_padding_stripped():
    body = b"hello"
    # padding count byte (3) includes itself: 3 trailing bytes removed
    raw = bytes([0xA0, 0]) + struct.pack("!HII", 1, 2, 3) + body + b"\x03\x03\x03"
    pkt = parse_rtp(raw, 0)
    assert pkt.padding is True
    assert pkt.payload == b"hello"


def test_audio_decode_shape_and_range():
    payload = bytes([0, 128, 255])
    raw = build_rtp(0, 0, 1, payload)
    audio = parse_rtp(raw, 0).decode_audio()
    assert audio.dtype == np.float32
    assert audio[0] == pytest.approx(-1.0)
    assert audio[1] == pytest.approx(0.0)
    assert audio[2] == pytest.approx(127 / 128, abs=1e-6)


@pytest.mark.parametrize("data,reason", [
    (b"\x80\x00", "TRUNCATED_HEADER"),
    (bytes([0x40]) + b"\x00" * 11, "BAD_VERSION"),
])
def test_parse_failures(data, reason):
    with pytest.raises(RtpParseError) as exc:
        parse_rtp(data, 0)
    assert exc.value.reason == reason


def test_bad_padding_length():
    raw = (bytes([0xA0, 0]) + struct.pack("!HII", 1, 2, 3)
           + b"\x01\x09")  # claims 9 padding bytes in a 2-byte payload
    with pytest.raises(RtpParseError) as exc:
        parse_rtp(raw, 0)
    assert exc.value.reason == "BAD_PADDING"
