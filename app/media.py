"""Media parsing: WAV/RIFF container parsing and raw PCM decoding.

Scope is deliberately narrow and explicit (see README "Supported scope"):
  * WAVE/RF64-less RIFF containers with PCM (0x0001), IEEE float (0x0003) or
    WAVE_FORMAT_EXTENSIBLE (0xFFFE) whose sub-format is PCM/float;
  * raw interleaved little-endian PCM for the streaming endpoint, with the
    format declared by the client;
  * 16/24/32-bit integer and 32/64-bit float samples;
  * the normative 48 kHz rate only; 1, 2 or 6 channels.

Nothing here resamples, decompresses or looks at external/network content.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

from .errors import (
    InvalidMediaError,
    UnsupportedFormatError,
    UnsupportedLayoutError,
    UnsupportedSampleRateError,
)
from .r128_constants import SAMPLE_RATE_HZ

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

# bytes per sample for the raw formats we understand
SAMPLE_WIDTHS = {"s16": 2, "s24": 3, "s32": 4, "f32": 4, "f64": 8}
_ALLOWED_CHANNELS = (1, 2, 6)

# PCM sub-format GUID inside WAVE_FORMAT_EXTENSIBLE: the first two bytes repeat
# the legacy format tag; the remaining bytes are this fixed suffix.
_KSDATAFORMAT_SUBTYPE_SUFFIX = bytes.fromhex(
    "0000aa00389b71"
)  # 00 00 aa 00 38 9b 71 (8 bytes following the 2-byte tag)


@dataclass(frozen=True)
class ParsedAudio:
    samples: np.ndarray            # float64, shape (frames, channels)
    sample_rate: int
    channels: int
    sample_format: str             # s16 | s24 | s32 | f32 | f64
    is_extensible: bool


def frame_size(sample_format: str, channels: int) -> int:
    if sample_format not in SAMPLE_WIDTHS:
        raise UnsupportedFormatError(
            f"unknown sample format {sample_format!r}; "
            f"expected one of {sorted(SAMPLE_WIDTHS)}"
        )
    return SAMPLE_WIDTHS[sample_format] * channels


def decode_pcm(buf: bytes, sample_format: str, channels: int) -> tuple[np.ndarray, bytes]:
    """Decode interleaved little-endian PCM to float64 frames.

    Returns ``(samples, leftover)`` where ``leftover`` holds the trailing bytes
    that do not form a complete frame (kept by the streaming job and prepended
    to the next chunk). Never raises on a non-empty leftover.
    """
    fs = frame_size(sample_format, channels)
    usable = (len(buf) // fs) * fs
    if usable == 0:
        return np.empty((0, channels), dtype=np.float64), bytes(buf)
    frame_bytes = bytes(buf[:usable])
    leftover = bytes(buf[usable:])
    samples = _decode_aligned(frame_bytes, sample_format, channels)
    return samples, leftover


def _decode_aligned(buf: bytes, sample_format: str, channels: int) -> np.ndarray:
    width = SAMPLE_WIDTHS[sample_format]
    total_frames = len(buf) // (width * channels)
    if sample_format == "s16":
        x = np.frombuffer(buf, dtype="<i2").astype(np.float64) / 32768.0
    elif sample_format == "s32":
        x = np.frombuffer(buf, dtype="<i4").astype(np.float64) / 2147483648.0
    elif sample_format == "f32":
        x = np.frombuffer(buf, dtype="<f4").astype(np.float64)
    elif sample_format == "f64":
        x = np.frombuffer(buf, dtype="<f8").copy()
    elif sample_format == "s24":
        n_scalar = len(buf) // width
        x = _decode_s24(buf, n_scalar)
    else:  # pragma: no cover - guarded by frame_size
        raise UnsupportedFormatError(f"unsupported sample format {sample_format!r}")
    return x.reshape(total_frames, channels)


def _decode_s24(buf: bytes, total_samples: int) -> np.ndarray:
    raw = np.frombuffer(buf, dtype=np.uint8).reshape(total_samples, 3)
    # little-endian signed two's complement, sign-extended to int32
    value = raw[:, 0].astype(np.int32) \
        | (raw[:, 1].astype(np.int32) << 8) \
        | (raw[:, 2].astype(np.int32) << 16)
    value[value >= 0x800000] -= 0x1000000
    return value / 8388608.0


def parse_wav(buf: bytes) -> ParsedAudio:
    """Parse a complete WAV byte string into decoded float64 samples.

    Validates the container itself rather than trusting the client; raises an
    :class:`R128Error` subclass on every structural problem.
    """
    if len(buf) < 12 or buf[0:4] != b"RIFF" or buf[8:12] != b"WAVE":
        raise InvalidMediaError("not a RIFF/WAVE container")
    declared_size = struct.unpack_from("<I", buf, 4)[0]
    if declared_size and declared_size + 8 > len(buf) + 1:
        # A truncated upload: header promises more bytes than arrived.
        raise InvalidMediaError(
            "truncated WAV payload",
            details={"declared_bytes": declared_size + 8, "actual_bytes": len(buf)},
        )

    fmt = _find_chunk(buf, 12, b"fmt ")
    data = _find_chunk(buf, 12, b"data")
    if fmt is None:
        raise InvalidMediaError("missing 'fmt ' chunk")
    if data is None:
        raise InvalidMediaError("missing 'data' chunk")

    tag, channels, sample_rate, byte_rate, block_align, bits = \
        struct.unpack_from("<HHIIHH", fmt.payload, 0)
    is_extensible = False
    if tag == WAVE_FORMAT_EXTENSIBLE:
        is_extensible = True
        if len(fmt.payload) < 40:
            raise InvalidMediaError("WAVE_FORMAT_EXTENSIBLE fmt chunk too short")
        # cbSize(22) + validbits(2) + channelmask(4) + SUBTYPE GUID(16)
        sub_guid = fmt.payload[24:40]
        tag = struct.unpack_from("<H", sub_guid, 0)[0]
        if sub_guid[2:] != _KSDATAFORMAT_SUBTYPE_SUFFIX:
            raise UnsupportedFormatError("unrecognised EXTENSIBLE sub-format GUID")
    if tag == WAVE_FORMAT_PCM:
        if bits not in (16, 24, 32):
            raise UnsupportedFormatError(
                f"integer PCM bit depth {bits} not supported (16/24/32 only)"
            )
        sample_format = {16: "s16", 24: "s24", 32: "s32"}[bits]
    elif tag == WAVE_FORMAT_IEEE_FLOAT:
        if bits == 32:
            sample_format = "f32"
        elif bits == 64:
            sample_format = "f64"
        else:
            raise UnsupportedFormatError(
                f"float bit depth {bits} not supported (32/64 only)"
            )
    else:
        raise UnsupportedFormatError(
            "compressed or non-PCM audio is not supported",
            details={"format_tag": f"0x{tag:04x}"},
        )

    if sample_rate != SAMPLE_RATE_HZ:
        raise UnsupportedSampleRateError(
            f"sample rate {sample_rate} Hz is not supported; this backend only "
            f"measures the normative {SAMPLE_RATE_HZ} Hz rate and does not "
            "resample. Resample to 48 kHz (e.g. ffmpeg -ar 48000) first.",
            details={"sample_rate": sample_rate},
        )
    if channels not in _ALLOWED_CHANNELS:
        raise UnsupportedLayoutError(
            f"{channels} channels not supported; allowed counts are "
            f"{list(_ALLOWED_CHANNELS)} (mono/dual-mono, stereo, 5.1)",
            details={"channels": channels},
        )

    width = SAMPLE_WIDTHS[sample_format]
    if block_align and block_align != width * channels:
        raise InvalidMediaError(
            "inconsistent block align",
            details={"block_align": block_align, "expected": width * channels},
        )
    if byte_rate and byte_rate != SAMPLE_RATE_HZ * width * channels:
        raise InvalidMediaError(
            "inconsistent byte rate",
            details={"byte_rate": byte_rate,
                     "expected": SAMPLE_RATE_HZ * width * channels},
        )
    if len(data.payload) % (width * channels) != 0:
        raise InvalidMediaError(
            "data chunk length is not a whole number of inter-channel frames",
            details={"data_bytes": len(data.payload),
                     "frame_size": width * channels},
        )

    samples = _decode_aligned(data.payload, sample_format, channels)
    return ParsedAudio(
        samples=samples,
        sample_rate=sample_rate,
        channels=channels,
        sample_format=sample_format,
        is_extensible=is_extensible,
    )


class _Chunk:
    __slots__ = ("payload",)

    def __init__(self, payload: bytes):
        self.payload = payload


def _find_chunk(buf: bytes, start: int, cid: bytes) -> _Chunk | None:
    pos = start
    n = len(buf)
    while pos + 8 <= n:
        chunk_id = buf[pos:pos + 4]
        size = struct.unpack_from("<I", buf, pos + 4)[0]
        body_start = pos + 8
        if body_start + size > n:
            if chunk_id == b"data":
                # Some recorders write a trailing data chunk size larger than
                # the file as captured; treat what actually arrived as data.
                size = n - body_start
            else:
                raise InvalidMediaError(
                    f"chunk {chunk_id!r} header extends past end of file",
                    details={"chunk": chunk_id.decode('ascii', 'replace'),
                             "declared": size},
                )
        if chunk_id == cid:
            return _Chunk(bytes(buf[body_start:body_start + size]))
        pos = body_start + size + (size & 1)  # chunks are word-aligned (pad)
    return None
