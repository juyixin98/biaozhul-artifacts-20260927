"""媒体解析契约：PCM 各格式、WAV、多声道混合、畸形输入分类。"""
from __future__ import annotations

import struct

import numpy as np
import pytest

from app.errors import InputInvalidError
from app.media import decode_pcm, parse_wav, wav_bytes


def test_decode_s16_roundtrip_levels():
    samples = np.array([0.0, 0.5, -0.5, 1.0, -1.0])
    pcm = (samples * 32767.0).astype("<i2").tobytes()
    mono, frames = decode_pcm(pcm, "s16", 1)
    assert frames == 5
    assert mono.dtype == np.float64
    np.testing.assert_allclose(mono, samples, atol=1e-4)


def test_decode_s24_and_s32():
    x = np.array([0.25, -0.75, 1.0])
    pcm24 = b"".join(
        int(max(-2 ** 23, min(2 ** 23 - 1, round(v * (1 << 23)))))
        .to_bytes(3, "little", signed=True) for v in x)
    mono, frames = decode_pcm(pcm24, "s24", 1)
    assert frames == 3
    np.testing.assert_allclose(mono, x, atol=1e-6)

    pcm32 = (x * (2 ** 31 - 1)).astype("<i4").tobytes()
    mono32, f32 = decode_pcm(pcm32, "s32", 1)
    assert f32 == 3
    np.testing.assert_allclose(mono32, x, atol=1e-9)


def test_decode_f32():
    x = np.array([0.1, -0.2, 0.3], dtype="<f4")
    mono, frames = decode_pcm(x.tobytes(), "f32", 1)
    assert frames == 3
    np.testing.assert_allclose(mono, x.astype(np.float64), atol=1e-7)


def test_stereo_downmix_is_mean_not_max():
    # 帧1: (0.8, 0.0) -> 均值 0.4；若误取 max 会是 0.8
    frame = np.array([0.8, 0.0, -0.8, 0.0], dtype="<f4")
    mono, frames = decode_pcm(frame.tobytes(), "f32", 2)
    assert frames == 2
    np.testing.assert_allclose(mono, [0.4, -0.4], atol=1e-7)


def test_non_frame_aligned_reports_consumed_and_remainder():
    # s16 stereo: frame_size=4；给 9 字节
    with pytest.raises(InputInvalidError) as ei:
        decode_pcm(b"\0" * 9, "s16", 2)
    d = ei.value.details
    assert d["frame_size"] == 4
    assert d["bytes_consumed"] == 8
    assert d["remainder"] == 1


def test_unknown_format_and_bad_channels():
    with pytest.raises(InputInvalidError):
        decode_pcm(b"\0" * 4, "mulaw", 1)
    with pytest.raises(InputInvalidError):
        decode_pcm(b"\0" * 4, "s16", 0)
    with pytest.raises(InputInvalidError):
        decode_pcm(b"\0" * 4, "s16", 9)


def test_wav_roundtrip():
    x = np.zeros(300)
    x[50:150] = 0.5
    blob = wav_bytes(x, 8000)
    mono, sr, ch, frames = parse_wav(blob)
    assert (sr, ch, frames) == (8000, 1, 300)
    np.testing.assert_allclose(mono, x, atol=1e-4)


def test_wav_rejects_non_wav():
    with pytest.raises(InputInvalidError):
        parse_wav(b"not a wav file at all")
    with pytest.raises(InputInvalidError):
        parse_wav(b"")


def test_wav_truncated_chunk_rejected():
    blob = bytearray(wav_bytes(np.zeros(100), 8000))
    # 定位 data chunk："data" 标识在其长度字段前 4 字节
    off = blob.index(b"data")
    size_field = off + 4
    total = len(blob)
    struct.pack_into("<I", blob, size_field,
                     total - (size_field + 4) + 50)
    with pytest.raises(InputInvalidError) as ei:
        parse_wav(bytes(blob))
    assert ei.value.details["declared_size"] > ei.value.details["file_size"]


def test_wav_missing_chunks():
    # RIFF/WAVE 但没有 fmt/data
    riff = b"RIFF" + struct.pack("<I", 4) + b"WAVE"
    with pytest.raises(InputInvalidError):
        parse_wav(riff)


def test_wav_unsupported_format():
    # 8-bit PCM (tag=1, bits=8) 不支持
    data = b"\x80" * 10
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 8000, 1, 8)
    blob = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
            + b"fmt " + struct.pack("<I", 16) + fmt
            + b"data" + struct.pack("<I", len(data)) + data)
    with pytest.raises(InputInvalidError) as ei:
        parse_wav(blob)
    assert ei.value.details["bits"] == 8
