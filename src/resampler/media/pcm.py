"""Raw PCM codecs for mono sample transport.

Supported sample formats (little-endian where multi-byte):

    u8      unsigned 8-bit integer, center 128
    s16le   signed 16-bit
    s24le   signed 24-bit (packed 3 bytes)
    s32le   signed 32-bit
    f32le   IEEE-754 binary32
    f64le   IEEE-754 binary64

Integer conversion convention: float value v in [-1, 1] maps to the
nearest representable level (round-half-to-even, NumPy default); values
outside range are handled per policy ("clip" saturate + count,
"reject" raise OutputOverflowError).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..errors import InputValidationError, OutputOverflowError

PCM_FORMATS = {
    "u8":    {"bytes": 1, "kind": "uint",  "dtype": np.uint8},
    "s16le": {"bytes": 2, "kind": "sint",  "dtype": np.int16},
    "s24le": {"bytes": 3, "kind": "s24",   "dtype": None},
    "s32le": {"bytes": 4, "kind": "sint",  "dtype": np.int32},
    "f32le": {"bytes": 4, "kind": "float", "dtype": np.float32},
    "f64le": {"bytes": 8, "kind": "float", "dtype": np.float64},
}
INT_FORMATS = {"u8", "s16le", "s24le", "s32le"}
FLOAT_FORMATS = {"f32le", "f64le"}
CLIP_POLICIES = {"clip", "reject"}


@dataclass(frozen=True)
class EncodeResult:
    data: bytes
    samples: int
    clipped: int
    policy: str


def sample_width(fmt: str) -> int:
    try:
        return PCM_FORMATS[fmt]["bytes"]
    except KeyError:
        raise InputValidationError("unknown PCM format",
                                   {"format": fmt, "known": sorted(PCM_FORMATS)})


def decode_pcm(raw: bytes | bytearray, fmt: str) -> np.ndarray:
    """Decode raw little-endian PCM bytes to mono float64 samples."""
    spec = PCM_FORMATS.get(fmt)
    if spec is None:
        raise InputValidationError("unknown PCM format", {"format": fmt})
    width = spec["bytes"]
    if len(raw) % width != 0:
        raise InputValidationError(
            "PCM byte length not aligned to sample width",
            {"bytes": len(raw), "width": width, "format": fmt})
    if not raw:
        return np.empty(0, dtype=np.float64)

    kind = spec["kind"]
    if kind == "uint":
        u = np.frombuffer(raw, dtype=np.uint8).astype(np.float64)
        x = (u - 128.0) / 128.0
    elif kind == "sint":
        dt = spec["dtype"]
        i = np.frombuffer(raw, dtype=dt).astype(np.float64)
        x = i / float(np.iinfo(dt).max)
    elif kind == "s24":
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        i32 = (b[:, 0].astype(np.int32)
               | (b[:, 1].astype(np.int32) << 8)
               | (b[:, 2].astype(np.int32) << 16))
        i32 = np.where(i32 & 0x800000, i32 - 0x1000000, i32)
        x = i32.astype(np.float64) / float(1 << 23)
    else:
        x = np.frombuffer(raw, dtype=spec["dtype"]).astype(np.float64)

    if not np.all(np.isfinite(x)):
        bad = int(np.sum(~np.isfinite(x)))
        raise InputValidationError(
            "non-finite sample in PCM input",
            {"format": fmt, "bad_samples": bad})
    return np.ascontiguousarray(x, dtype=np.float64)


def encode_pcm(x: np.ndarray, fmt: str, clip_policy: str = "clip") -> EncodeResult:
    """Encode mono float64 samples to raw little-endian PCM."""
    if fmt not in PCM_FORMATS:
        raise InputValidationError("unknown PCM format", {"format": fmt})
    if clip_policy not in CLIP_POLICIES:
        raise InputValidationError("invalid clip policy", {"policy": clip_policy})
    if x.ndim != 1:
        raise InputValidationError("encode expects 1-D mono samples", {"ndim": x.ndim})
    if x.size and not np.all(np.isfinite(x)):
        bad = int(np.sum(~np.isfinite(x)))
        raise OutputOverflowError(
            "non-finite value cannot be encoded",
            {"format": fmt, "bad_samples": bad})

    spec = PCM_FORMATS[fmt]
    kind = spec["kind"]

    if kind == "float":
        out = x.astype(spec["dtype"], copy=True)
        # f32 cast of huge but finite f64 could yield inf:
        if out.size and not np.all(np.isfinite(out)):
            bad = int(np.sum(~np.isfinite(out)))
            if clip_policy == "reject":
                raise OutputOverflowError(
                    "float cast overflow", {"format": fmt, "bad_samples": bad})
            finite_max = np.finfo(spec["dtype"]).max
            out = np.nan_to_num(np.clip(x, -finite_max, finite_max)).astype(spec["dtype"])
            return EncodeResult(out.tobytes(), out.size, bad, clip_policy)
        return EncodeResult(out.tobytes(), out.size, 0, clip_policy)

    if kind == "uint":
        peak = 128
        scaled = np.round(x * 128.0)
        over = np.abs(scaled) > 128
        n_clip = int(np.sum(over))
        if n_clip and clip_policy == "reject":
            raise OutputOverflowError(
                "samples out of u8 range", {"format": fmt, "clipped": n_clip,
                                            "max_abs": float(np.max(np.abs(x)))})
        sat = np.clip(scaled, -128, 127).astype(np.int32)
        out = (sat + 128).astype(np.uint8)
    elif kind == "sint":
        dt = spec["dtype"]
        info = np.iinfo(dt)
        scale = float(info.max)
        scaled = np.round(x * scale)
        over = (scaled < info.min) | (scaled > info.max)
        n_clip = int(np.sum(over))
        if n_clip and clip_policy == "reject":
            raise OutputOverflowError(
                f"samples out of {fmt} range",
                {"format": fmt, "clipped": n_clip,
                 "max_abs": float(np.max(np.abs(x)))})
        out = np.clip(scaled, info.min, info.max).astype(dt)
    else:  # s24
        scale = float(1 << 23)
        scaled = np.round(x * scale)
        over = (scaled < -(1 << 23)) | (scaled > (1 << 23) - 1)
        n_clip = int(np.sum(over))
        if n_clip and clip_policy == "reject":
            raise OutputOverflowError(
                "samples out of s24 range", {"format": fmt, "clipped": n_clip,
                                             "max_abs": float(np.max(np.abs(x)))})
        sat = np.clip(scaled, -(1 << 23), (1 << 23) - 1).astype(np.int32)
        u = sat & 0xFFFFFF
        out = np.empty((sat.size, 3), dtype=np.uint8)
        out[:, 0] = u & 0xFF
        out[:, 1] = (u >> 8) & 0xFF
        out[:, 2] = (u >> 16) & 0xFF
        return EncodeResult(out.reshape(-1).tobytes(), sat.size, n_clip, clip_policy)

    return EncodeResult(out.tobytes(), out.size, n_clip, clip_policy)
