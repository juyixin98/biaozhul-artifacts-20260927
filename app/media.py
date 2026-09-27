"""Media parsing: RTP packet model, RFC 3550 header parser, payload decode.

Only the subset needed for offline audio analysis is implemented:
  - fixed 12-byte RTP header parsing (CSRC count honoured)
  - sequence number / timestamp / SSRC extraction
  - 8-bit PCM (PCMU-style octets) -> float32 [-1, 1] payload decoding

Padding and extension bits are validated, not silently ignored, so malformed
fixtures produce explicit parse errors with a reason code.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

RTP_VERSION = 2
RTP_HEADER_LEN = 12


class RtpParseError(ValueError):
    """Raised with a stable ``reason`` code for malformed packets."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class RtpPacket:
    version: int
    padding: bool
    extension: bool
    marker: bool
    payload_type: int
    seq: int            # raw 16-bit value 0..65535
    timestamp: int      # raw 32-bit value 0..2^32-1
    ssrc: int
    payload: bytes
    arrival_ms: float   # receiver wall-clock arrival (synthetic in fixtures)

    def decode_audio(self) -> np.ndarray:
        """Decode 8-bit unsigned PCM octets to float32 mono in [-1, 1]."""
        if not self.payload:
            return np.zeros(0, dtype=np.float32)
        raw = np.frombuffer(self.payload, dtype=np.uint8).astype(np.float32)
        return (raw - 128.0) / 128.0


def parse_rtp(data: bytes, arrival_ms: float) -> RtpPacket:
    """Parse one RTP datagram. Raises :class:`RtpParseError` on bad input."""
    if len(data) < RTP_HEADER_LEN:
        raise RtpParseError("TRUNCATED_HEADER", f"got {len(data)} bytes")

    b0, b1, seq, timestamp, ssrc = struct.unpack("!BBHII", data[:12])
    version = (b0 >> 6) & 0x3
    if version != RTP_VERSION:
        raise RtpParseError("BAD_VERSION", f"version={version}")

    padding = bool(b0 & 0x20)
    extension = bool(b0 & 0x10)
    csrc_count = b0 & 0x0F
    marker = bool(b1 & 0x80)
    payload_type = b1 & 0x7F

    offset = RTP_HEADER_LEN + 4 * csrc_count
    if extension:
        if len(data) < offset + 4:
            raise RtpParseError("TRUNCATED_EXTENSION", "")
        ext_len_words = struct.unpack("!H", data[offset + 2:offset + 4])[0]
        offset += 4 + 4 * ext_len_words
        if len(data) < offset:
            raise RtpParseError("TRUNCATED_EXTENSION", "length exceeds packet")

    payload = bytes(data[offset:])
    if padding:
        if not payload:
            raise RtpParseError("BAD_PADDING", "empty padded payload")
        pad_len = payload[-1]
        if pad_len <= 0 or pad_len > len(payload):
            raise RtpParseError("BAD_PADDING", f"pad_len={pad_len}")
        payload = payload[:-pad_len]

    return RtpPacket(
        version=version,
        padding=padding,
        extension=extension,
        marker=bool(marker),
        payload_type=payload_type,
        seq=seq,
        timestamp=timestamp,
        ssrc=ssrc,
        payload=payload,
        arrival_ms=float(arrival_ms),
    )


def build_rtp(seq: int, timestamp: int, ssrc: int, payload: bytes,
              payload_type: int = 0, marker: bool = False) -> bytes:
    """Serialize an RTP packet (inverse of parse, used by fixtures/tests)."""
    b0 = (RTP_VERSION << 6)
    b1 = (0x80 if marker else 0) | (payload_type & 0x7F)
    return struct.pack("!BBHII", b0, b1, seq & 0xFFFF, timestamp & 0xFFFFFFFF,
                       ssrc & 0xFFFFFFFF) + payload
