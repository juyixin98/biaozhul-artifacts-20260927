"""Tests for media parsing: format coverage and explicit failure categories."""
from __future__ import annotations

import numpy as np
import pytest

from app import media
from app.errors import (
    InvalidMediaError,
    UnsupportedFormatError,
    UnsupportedLayoutError,
    UnsupportedSampleRateError,
)
from tests.fixtures import FS, to_wav


def test_decode_s16_roundtrip():
    x = np.array([[0.0], [1.0], [-1.0], [0.5], [-0.5]])
    wav = to_wav(x, "s16")
    parsed = media.parse_wav(wav)
    assert parsed.sample_format == "s16"
    assert parsed.samples.shape == x.shape
    assert parsed.sample_rate == FS
    np.testing.assert_allclose(parsed.samples, x, atol=1 / 32768)


@pytest.mark.parametrize("fmt", ["s16", "s24", "s32", "f32", "f64"])
def test_all_sample_formats_decode_and_preserve_level(fmt):
    x = np.linspace(-0.9, 0.9, FS // 10)[:, None]
    parsed = media.parse_wav(to_wav(x, fmt))
    assert parsed.sample_format == fmt
    # integer PCM widening should converge; float formats should be near exact
    tol = {"s16": 2e-5, "s24": 2e-7, "s32": 2e-9,
           "f32": 2e-7, "f64": 1e-12}[fmt]
    # s16 step size is 1/32768 ~ 3.05e-5
    if fmt == "s16":
        tol = 3.3e-5
    np.testing.assert_allclose(parsed.samples, x, atol=tol)


def test_s24_sign_extension():
    # a negative value must not be decoded as a large positive integer
    x = np.array([[-0.75], [0.75]])
    parsed = media.parse_wav(to_wav(x, "s24"))
    assert parsed.samples[0, 0] < 0
    assert parsed.samples[1, 0] > 0
    np.testing.assert_allclose(parsed.samples[:, 0], [-0.75, 0.75], atol=1e-6)


def test_stereo_and_51_channel_counts():
    for ch in (1, 2, 6):
        x = np.zeros((FS // 5, ch))
        parsed = media.parse_wav(to_wav(x))
        assert parsed.channels == ch
        assert parsed.samples.shape == (FS // 5, ch)


def test_non_wav_is_invalid_media():
    with pytest.raises(InvalidMediaError) as ei:
        media.parse_wav(b"this is definitely not a wav file")
    assert ei.value.code == "INVALID_MEDIA"


def test_truncated_wav_is_invalid_media():
    good = to_wav(np.zeros((FS, 1)))
    with pytest.raises(InvalidMediaError) as ei:
        media.parse_wav(good[: len(good) // 2])
    assert ei.value.code == "INVALID_MEDIA"


def test_non_48k_rejected_with_resample_guidance():
    wav = to_wav(np.zeros((1000, 1)))
    # rewrite the sample-rate field (offset 24 in fmt chunk)
    ba = bytearray(wav)
    ba[24:28] = (44100).to_bytes(4, "little")
    with pytest.raises(UnsupportedSampleRateError) as ei:
        media.parse_wav(bytes(ba))
    assert ei.value.code == "UNSUPPORTED_SAMPLE_RATE"
    assert "48" in ei.value.message


def test_three_channels_unsupported_layout():
    with pytest.raises(UnsupportedLayoutError) as ei:
        media.parse_wav(to_wav(np.zeros((100, 3))))
    assert ei.value.code == "UNSUPPORTED_LAYOUT"


def test_compressed_format_tag_rejected():
    # craft a minimal RIFF with an mp3-style tag (0x0055)
    import struct
    fmt_chunk = struct.pack("<HHIIHH", 0x0055, 1, FS, FS * 2, 2, 16)
    riff = b"RIFF" + struct.pack("<I", 0) + b"WAVE"
    riff += b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk
    riff += b"data" + struct.pack("<I", 0)
    with pytest.raises(UnsupportedFormatError) as ei:
        media.parse_wav(riff)
    assert ei.value.code == "UNSUPPORTED_FORMAT"
    assert ei.value.details["format_tag"] == "0x0055"


def test_raw_pcm_streaming_leftover():
    # 5 bytes of s16 mono = 2 whole samples + 1 byte held for the next chunk
    samples, leftover = media.decode_pcm(b"\x00\x00\xab\xcd\xef", "s16", 1)
    assert samples.shape == (2, 1)
    assert leftover == b"\xef"


def test_raw_pcm_empty_until_a_frame_accumulates():
    # a single byte cannot form an s16 sample: nothing decoded, all held
    samples, leftover = media.decode_pcm(b"\x12", "s16", 1)
    assert samples.shape == (0, 1)
    assert leftover == b"\x12"


def test_raw_pcm_multichannel_frame_boundary():
    # s16 stereo frame = 4 bytes; 6 bytes -> 1 frame + 2 leftover
    samples, leftover = media.decode_pcm(b"\x00" * 6, "s16", 2)
    assert samples.shape == (1, 2)
    assert len(leftover) == 2
