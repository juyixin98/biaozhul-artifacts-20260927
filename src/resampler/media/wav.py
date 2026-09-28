"""Minimal mono RIFF/WAVE reader and writer (no external deps).

Accepted input: uncompressed PCM (format code 1) or IEEE float (code 3,
typically with ``fact`` chunk), 1 channel.  Everything else (compressed
formats, multichannel, RF64, missing data chunk) is rejected as an
input/media error rather than guessed at.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

from ..errors import InputValidationError
from .pcm import sample_width

_FORMAT_TO_PCM = {
    1: {8: "u8", 16: "s16le", 24: "s24le", 32: "s32le"},
    3: {32: "f32le", 64: "f64le"},
}
_PCM_TO_FORMAT = {"u8": (1, 8), "s16le": (1, 16), "s24le": (1, 24),
                  "s32le": (1, 32), "f32le": (3, 32), "f64le": (3, 64)}


@dataclass(frozen=True)
class WaveInfo:
    sample_rate: int
    channels: int
    pcm_format: str
    n_frames: int


@dataclass(frozen=True)
class WaveDecodeResult:
    samples: np.ndarray      # mono float64
    info: WaveInfo


def _iter_chunks(buf: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        cid = buf[pos:pos + 4]
        (size,) = struct.unpack_from("<I", buf, pos + 4)
        body = pos + 8
        if body + size > end:
            raise InputValidationError("WAV chunk exceeds container",
                                       {"chunk": cid.decode("ascii", "replace"),
                                        "size": size})
        yield cid, buf[body:body + size]
        pos = body + size + (size & 1)


def parse_wav(buf: bytes) -> WaveDecodeResult:
    if len(buf) < 12 or buf[0:4] != b"RIFF" or buf[8:12] != b"WAVE":
        raise InputValidationError("not a RIFF/WAVE file", {"head": buf[:12].hex()})
    riff_size = struct.unpack_from("<I", buf, 4)[0]
    end = min(len(buf), 8 + riff_size)

    fmt_body = None
    data_body = None
    for cid, body in _iter_chunks(buf, 12, end):
        if cid == b"fmt ":
            fmt_body = body
        elif cid == b"data":
            data_body = body

    if fmt_body is None or len(fmt_body) < 16:
        raise InputValidationError("WAV missing fmt chunk")
    if data_body is None:
        raise InputValidationError("WAV missing data chunk")

    format_tag, channels, sample_rate, _byte_rate, _block_align, bits = \
        struct.unpack_from("<HHIIHH", fmt_body, 0)
    if channels != 1:
        raise InputValidationError("only mono WAV is supported",
                                   {"channels": channels})
    if sample_rate <= 0:
        raise InputValidationError("invalid WAV sample rate",
                                   {"sample_rate": sample_rate})
    table = _FORMAT_TO_PCM.get(format_tag)
    if table is None or bits not in table:
        raise InputValidationError(
            "unsupported WAV encoding (PCM/int or IEEE float only)",
            {"format_tag": format_tag, "bits": bits})
    pcm_format = table[bits]
    width = sample_width(pcm_format)
    if len(data_body) % width != 0:
        raise InputValidationError(
            "WAV data payload not aligned to sample width",
            {"bytes": len(data_body), "width": width})

    # Local import: media codecs live together but keep pcm reusable standalone.
    from .pcm import decode_pcm
    samples = decode_pcm(bytes(data_body), pcm_format)
    info = WaveInfo(sample_rate=sample_rate, channels=1,
                    pcm_format=pcm_format, n_frames=samples.size)
    return WaveDecodeResult(samples=samples, info=info)


def build_wav(samples: np.ndarray, sample_rate: int, pcm_format: str) -> bytes:
    """Serialize mono float64 samples into a RIFF/WAVE byte string."""
    from .pcm import encode_pcm
    if samples.ndim != 1:
        raise InputValidationError("only mono WAV can be written",
                                   {"ndim": samples.ndim})
    format_tag, bits = _PCM_TO_FORMAT[pcm_format]
    encoded = encode_pcm(samples, pcm_format, clip_policy="clip")
    width = sample_width(pcm_format)
    block_align = width
    byte_rate = sample_rate * block_align
    n = samples.size
    fact = b""
    if format_tag == 3:
        fact = b"fact" + struct.pack("<II", 4, n)
    fmt_body = struct.pack(
        "<HHIIHH", format_tag, 1, sample_rate, byte_rate, block_align, bits)
    fmt_chunk = b"fmt " + struct.pack("<I", 16) + fmt_body
    data_chunk = b"data" + struct.pack("<I", len(encoded.data)) + encoded.data
    body = fmt_chunk + fact + data_chunk
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body
