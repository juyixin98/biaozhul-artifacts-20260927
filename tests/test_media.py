"""Unit tests for WAV media parsing and format validation."""
from __future__ import annotations

import wave

import numpy as np
import pytest

from clockalign.errors import MediaError
from clockalign.media import (load_pair, load_track, read_wav,
                              write_pcm_wav)


@pytest.mark.parametrize("width", [1, 2, 3, 4])
def test_roundtrip_pcm_widths(tmp_path, width):
    fs = 8000
    t = np.arange(fs) / fs
    sig = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    path = tmp_path / f"w{width}.wav"
    write_pcm_wav(path, sig, fs, sampwidth=width)
    samples, rate, ch = read_wav(path)
    assert rate == fs and ch == 1
    mono = samples[:, 0] if samples.ndim == 2 else samples
    tol = {1: 0.03, 2: 2e-3, 3: 2e-4, 4: 1e-5}[width]
    np.testing.assert_allclose(mono, sig, atol=tol)


def test_stereo_channel_extraction(tmp_path):
    fs = 8000
    stereo = np.stack([np.full(fs, 0.25), np.full(fs, -0.25)], axis=1)
    path = tmp_path / "s.wav"
    write_pcm_wav(path, stereo, fs)
    ref = load_track(path, channel=0)
    slv = load_track(path, channel=1)
    assert abs(float(np.mean(ref.samples)) - 0.25) < 0.01
    assert abs(float(np.mean(slv.samples)) + 0.25) < 0.01


def test_load_pair_rejects_sample_rate_mismatch(tmp_path):
    write_pcm_wav(tmp_path / "a.wav", np.zeros(1000, np.float32), 16000)
    write_pcm_wav(tmp_path / "b.wav", np.zeros(1000, np.float32), 8000)
    with pytest.raises(MediaError) as exc:
        load_pair(tmp_path / "a.wav", tmp_path / "b.wav")
    assert "mismatch" in exc.value.message


def test_missing_file_is_media_error(tmp_path):
    with pytest.raises(MediaError):
        load_track(tmp_path / "nope.wav")


def test_compressed_wav_rejected(tmp_path):
    path = tmp_path / "c.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        # Compression type must be declared before frames are written.
        try:
            wf.setcomptype("ULAW", "ulaw")
        except wave.Error:
            pytest.skip("python wave cannot construct ULAW headers")
        wf.writeframes(b"\0" * 100)
    with pytest.raises(MediaError):
        read_wav(path)
