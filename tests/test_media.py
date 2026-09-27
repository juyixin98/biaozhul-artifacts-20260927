"""Media parsing tests: WAV (PCM 8/16/24/32, float32) and raw PCM, with
concrete failure categories.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.media import (
    MediaError,
    decode_raw_pcm,
    decode_wav,
)
from conftest import SR, sine, write_pcm_wav_bytes, write_wav_bytes


def _tone(dur=1.0, level=0.5, freq=1000.0):
    return sine(level, freq, dur).astype(np.float32)


def test_float_wav_roundtrip():
    x = _tone()
    decoded = decode_wav(write_wav_bytes(x, SR))
    assert decoded.sample_rate == SR
    assert decoded.source_channels == 1
    assert decoded.layout == "mono"
    np.testing.assert_allclose(decoded.samples[:, 0], x.astype(np.float64), atol=1e-7)
    assert decoded.channel_weights == (1.0,)


@pytest.mark.parametrize("bits", [8, 16, 24, 32])
def test_integer_pcm_wav_roundtrip(bits):
    x = _tone()
    decoded = decode_wav(write_pcm_wav_bytes(x, SR, bits))
    # Quantization error scales with the bit depth; 8-bit is coarse.
    tol = {8: 0.02, 16: 2e-4, 24: 2e-6, 32: 1e-9}[bits]
    np.testing.assert_allclose(decoded.samples[:, 0], x.astype(np.float64), atol=tol)
    expected_fmt = "wav:u8" if bits == 8 else f"wav:s{bits}"
    assert decoded.source_format == expected_fmt


def test_stereo_and_51_layout_selection():
    stereo = np.stack([_tone(), _tone(level=0.3, freq=440)], axis=1)
    d2 = decode_wav(write_wav_bytes(stereo, SR))
    assert d2.layout == "stereo"
    assert d2.channel_weights == (1.0, 1.0)

    six = np.zeros((SR, 6), dtype=np.float32)
    six[:, 0] = _tone()
    d6 = decode_wav(write_wav_bytes(six, SR))
    assert d6.layout == "5.1"
    assert d6.samples.shape[1] == 5          # LFE removed
    assert d6.source_channels == 6
    assert d6.channel_weights == (1.0, 1.0, 1.0, 1.41, 1.41)


def test_five_point1_lfe_dropped_value_check():
    t = np.arange(SR) / SR
    six = np.zeros((SR, 6), dtype=np.float32)
    six[:, 3] = 0.9 * np.sin(2 * np.pi * 80 * t)  # LFE only
    d = decode_wav(write_wav_bytes(six, SR))
    assert np.max(np.abs(d.samples)) == 0.0


def test_raw_pcm_s24_decode_and_layout():
    x = _tone(0.5)
    scaled = np.round(x * 8388607).astype("<i4")
    raw = np.stack([scaled & 0xFF, (scaled >> 8) & 0xFF,
                    (scaled >> 16) & 0xFF], axis=-1).astype(np.uint8).tobytes()
    decoded = decode_raw_pcm(raw, sample_rate=SR, channels=1, sample_format="s24")
    np.testing.assert_allclose(decoded.samples[:, 0], x.astype(np.float64), atol=2e-6)


def test_raw_pcm_truncated_payload_is_rejected():
    raw = b"\x00\x01\x02"  # not divisible by frame size
    with pytest.raises(MediaError) as exc:
        decode_raw_pcm(raw, sample_rate=SR, channels=2, sample_format="s16")
    assert exc.value.code == "PCM_TRUNCATED"


@pytest.mark.parametrize("header,code", [
    (b"", "WAV_TOO_SHORT"),
    (b"XXXX" + b"\x00" * 60, "WAV_NOT_RIFF"),
])
def test_wav_container_failures(header, code):
    with pytest.raises(MediaError) as exc:
        decode_wav(header)
    assert exc.value.code == code


def test_wav_unsupported_channel_count():
    # 3-channel WAV: no mono/stereo/5.0/5.1 mapping.
    three = np.zeros((SR, 3), dtype=np.float32)
    with pytest.raises(MediaError) as exc:
        decode_wav(write_wav_bytes(three, SR))
    assert exc.value.code == "UNSUPPORTED_CHANNEL_LAYOUT"


def test_layout_override_mismatch_rejected():
    x = _tone()
    with pytest.raises(MediaError) as exc:
        decode_wav(write_wav_bytes(x, SR), layout_override="stereo")
    assert exc.value.code == "LAYOUT_CHANNEL_MISMATCH"
