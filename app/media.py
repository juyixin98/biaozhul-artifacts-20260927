"""媒体解析：WAV / 原始 PCM 字节 -> float64 单声道样本 [-1, 1]。

契约:
- decode_pcm(payload, fmt, channels) -> (samples_float64_mono, frames, channels)
- parse_wav(payload) -> (samples_mono, sample_rate, channels, frames)
- 多声道按各声道幅度均值混合（mean，非取最大，能量可解释）。
- 任何截断/对齐/格式问题抛 InputInvalidError，带 bytes_consumed 等定位字段。
"""
from __future__ import annotations

import io
import struct

import numpy as np

from .errors import InputInvalidError

PCM_FORMATS = {
    "s16": (2, "<i2", 32768.0),
    "s24": (3, "s24", 1.0),
    "s32": (4, "<i4", 2147483648.0),
    "f32": (4, "<f4", 1.0),
}

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
WAVE_FORMAT_IEEE_FLOAT = 0x0003


# ---------------------------------------------------------------------------
# 原始 PCM
# ---------------------------------------------------------------------------


def _decode_s24(buf: bytes, n: int) -> np.ndarray:
    a = np.frombuffer(buf, dtype=np.uint8).reshape(n, 3).astype(np.int32)
    v = a[:, 0] | (a[:, 1] << 8) | (a[:, 2] << 16)
    v = np.where(v >= (1 << 23), v - (1 << 24), v)
    return v.astype(np.float64) / float(1 << 23)


def decode_pcm(payload: bytes, sample_format: str,
               channels: int) -> tuple[np.ndarray, int]:
    """返回 (mono_float64, frames)。payload 必须恰好帧对齐。"""
    if sample_format not in PCM_FORMATS:
        raise InputInvalidError(
            f"unsupported sample_format {sample_format!r}",
            {"allowed": sorted(PCM_FORMATS)})
    if not isinstance(channels, int) or isinstance(channels, bool) \
            or not (1 <= channels <= 8):
        raise InputInvalidError(
            "channels must be an integer in [1, 8]", {"channels": channels})
    bps, dtype, scale = PCM_FORMATS[sample_format]
    frame_size = bps * channels
    n_bytes = len(payload)
    if n_bytes % frame_size != 0:
        consumed = n_bytes - (n_bytes % frame_size)
        raise InputInvalidError(
            f"payload is not frame-aligned: {n_bytes} bytes, frame_size="
            f"{frame_size}",
            {"bytes_total": n_bytes, "frame_size": frame_size,
             "bytes_consumed": consumed,
             "remainder": n_bytes - consumed})
    frames = n_bytes // frame_size
    if frames == 0:
        return np.zeros(0, dtype=np.float64), 0
    try:
        if dtype == "s24":
            raw = _decode_s24(payload, frames * channels)
        else:
            raw = np.frombuffer(payload, dtype=dtype).astype(np.float64)
            raw /= scale
    except (ValueError, struct.error) as e:
        raise InputInvalidError(f"PCM decode failed: {e}") from e

    if channels > 1:
        raw = raw.reshape(frames, channels).mean(axis=1)
    mono = np.ascontiguousarray(raw, dtype=np.float64)
    if not np.all(np.isfinite(mono)):
        raise InputInvalidError("decoded samples contain NaN/Inf")
    peak = float(np.max(np.abs(mono))) if frames else 0.0
    if peak > 1.0 + 1e-9:
        # 整数格式理论上 <1；f32 允许超幅但要求有限——这里裁剪并上报
        mono = np.clip(mono, -1.0, 1.0)
    return mono, frames


# ---------------------------------------------------------------------------
# WAV（RIFF）解析
# ---------------------------------------------------------------------------


def _iter_chunks(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        cid = data[pos:pos + 4]
        (size,) = struct.unpack_from("<I", data, pos + 4)
        body = pos + 8
        if body + size > end:
            raise InputInvalidError(
                f"WAV chunk {cid!r} extends beyond file",
                {"chunk": cid.decode("ascii", "replace"),
                 "declared_size": size, "file_size": end})
        yield cid, body, size
        pos = body + size + (size & 1)  # 字对齐 padding


def parse_wav(payload: bytes) -> tuple[np.ndarray, int, int, int]:
    """返回 (mono_float64, sample_rate, channels, frames)。

    支持 PCM 16/24/32-bit 整型、IEEE float32；WAVE_FORMAT_EXTENSIBLE 取
    subformat 真实格式。多 chunk data 会拼接（极少见但合法）。
    """
    if len(payload) < 12 or payload[:4] != b"RIFF" \
            or payload[8:12] != b"WAVE":
        raise InputInvalidError("not a RIFF/WAVE file",
                                {"magic": payload[:12].hex()})
    riff_size = struct.unpack_from("<I", payload, 4)[0]
    end = min(len(payload), 8 + riff_size)

    fmt_info = None
    data_parts: list[bytes] = []
    for cid, body, size in _iter_chunks(payload, 12, end):
        if cid == b"fmt ":
            if size < 16:
                raise InputInvalidError("fmt chunk too short", {"size": size})
            tag, channels, sr, _byte_rate, _block, bits = \
                struct.unpack_from("<HHIIHH", payload, body)
            actual_tag = tag
            if tag == WAVE_FORMAT_EXTENSIBLE and size >= 40:
                actual_tag = struct.unpack_from("<H", payload, body + 24)[0]
            fmt_info = (actual_tag, channels, sr, bits)
        elif cid == b"data":
            data_parts.append(payload[body:body + size])

    if fmt_info is None:
        raise InputInvalidError("WAV missing fmt chunk")
    if not data_parts:
        raise InputInvalidError("WAV missing data chunk")
    tag, channels, sr, bits = fmt_info
    if not (1 <= channels <= 8):
        raise InputInvalidError(f"unsupported channel count {channels}")
    if not (1 <= sr <= 1_000_000):
        raise InputInvalidError(f"unsupported sample rate {sr}")

    pcm = b"".join(data_parts)
    if tag == WAVE_FORMAT_PCM:
        if bits == 16:
            sf = "s16"
        elif bits == 24:
            sf = "s24"
        elif bits == 32:
            sf = "s32"
        else:
            raise InputInvalidError(
                f"unsupported PCM bit depth {bits}", {"bits": bits})
    elif tag == WAVE_FORMAT_IEEE_FLOAT and bits == 32:
        sf = "f32"
    else:
        raise InputInvalidError(
            f"unsupported WAV format tag={tag:#06x} bits={bits}",
            {"format_tag": tag, "bits": bits})

    mono, frames = decode_pcm(pcm, sf, channels)
    return mono, sr, channels, frames


def wav_bytes(samples: np.ndarray, sample_rate: int,
              bits: int = 16) -> bytes:
    """测试/夹具辅助：把单声道 float 样本编码为最小合法 WAV。"""
    interleaved = np.asarray(samples).reshape(-1)
    n = interleaved.size
    channels = 1
    if bits == 16:
        pcm = np.clip(interleaved, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype("<i2").tobytes()
    elif bits == 32:
        pcm = (np.clip(interleaved, -1.0, 1.0)
               * 2147483647.0).astype("<i4").tobytes()
    else:
        raise ValueError("only bits=16/32 supported by helper")
    byte_rate = sample_rate * channels * (bits // 8)
    block = channels * (bits // 8)
    fmt = struct.pack("<HHIIHH", WAVE_FORMAT_PCM, channels, sample_rate,
                      byte_rate, block, bits)
    out = io.BytesIO()
    out.write(b"RIFF")
    out.write(struct.pack("<I", 36 + len(pcm)))
    out.write(b"WAVE")
    out.write(b"fmt ")
    out.write(struct.pack("<I", 16))
    out.write(fmt)
    out.write(b"data")
    out.write(struct.pack("<I", len(pcm)))
    out.write(pcm)
    return out.getvalue()
