"""Chunked replay must reproduce whole-signal statistics exactly.

These tests exist because a naive implementation could feed each chunk's RMS
through a loudness formula and still 'look' plausible. The streaming meter has
to carry filter state, sub-frame input and rolling windows correctly; we
compare a single push against many pushes at awkward boundaries down to the
individual per-channel block-energy level.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.loudness import StreamingLoudnessMeter
from app.media import decode_pcm
from tests import fixtures as fx


def _measure_chunked(samples: np.ndarray, frame_chunks: list[int],
                     roles=None):
    """Feed samples split into explicit *sizes* (last size is the tail)."""
    meter = StreamingLoudnessMeter(samples.shape[1], roles=roles)
    pos = 0
    for n in frame_chunks:
        end = min(pos + n, len(samples))
        if end > pos:
            meter.push(samples[pos:end])
        pos = end
    assert pos == len(samples)
    return meter.finalize()


@pytest.mark.parametrize("boundaries", [
    [4800],
    [4801],
    [7919],
    [3000, 8000, 15000, 40000, 100000],
    [2400, 7200, 12001, 41234, 90000],
    [997, 1999, 3137, 7777, 50000, 120000],
    [1, 2, 3],
])
def test_chunked_equals_whole_tone(boundaries):
    x = fx.calibrated_loudness_tone(3.7, -23.0, channels=2)
    whole = StreamingLoudnessMeter(2)
    whole.push(x)
    r_whole = whole.finalize()
    # feed in the listed boundary sizes, then whatever remains as a last chunk
    edges = boundaries + [len(x)]
    r_chunk = _measure_chunked(x, edges)
    assert r_chunk.integrated_loudness_lufs == \
        pytest.approx(r_whole.integrated_loudness_lufs, abs=1e-12)
    assert r_chunk.gating.blocks_total == r_whole.gating.blocks_total
    assert r_chunk.gating.blocks_above_absolute == \
        r_whole.gating.blocks_above_absolute
    assert r_chunk.lra.loudness_range_lu == r_whole.lra.loudness_range_lu
    assert r_chunk.lra.blocks_total == r_whole.lra.blocks_total


def test_byte_level_pcm_replay_with_mid_sample_boundaries():
    """Split at raw BYTE boundaries (mid-sample, mid-frame), decode streaming."""
    x = fx.segmented_programme([(4, -30), (4, -20)], gap_seconds=1,
                               channels=2)
    wav = fx.to_wav(x, "s16")
    pcm = wav[44:]  # hand-built RIFF header is exactly 44 bytes here
    assert len(pcm) % 4 == 0

    whole_meter = StreamingLoudnessMeter(2)
    s, _ = decode_pcm(pcm, "s16", 2)
    whole_meter.push(s)
    whole = whole_meter.finalize()

    chunked = StreamingLoudnessMeter(2)
    leftover = b""
    pos = 0
    stride = 1499  # odd byte stride, not aligned to 4-byte stereo frames
    while pos < len(pcm):
        piece = leftover + pcm[pos:pos + stride]
        pos += stride
        samples, leftover = decode_pcm(piece, "s16", 2)
        if samples.shape[0]:
            chunked.push(samples)
    assert leftover == b""  # total length is an exact number of frames
    result = chunked.finalize()

    assert result.integrated_loudness_lufs == \
        pytest.approx(whole.integrated_loudness_lufs, abs=1e-12)
    assert result.gating.blocks_above_relative == \
        whole.gating.blocks_above_relative
    assert result.lra.blocks_total == whole.lra.blocks_total
    assert result.lra.histogram == whole.lra.histogram


def test_trailing_samples_reported_not_swallowed():
    # 4800*4 + 137 samples: 137 trailing samples must be reported as dropped
    x = fx.calibrated_loudness_tone(0.0, -23.0)  # 0-length, rebuild below
    n = 4800 * 4 + 137
    x = (0.1 * np.ones((n, 1)))
    m = StreamingLoudnessMeter(1)
    m.push(x)
    r = m.finalize()
    assert r.signal.trailing_samples_dropped == 137
    assert any("137" in w for w in r.warnings)


def test_chunked_equals_whole_51_with_surround_weight():
    x = fx.surround_mix(5.0, -23.0)
    whole = StreamingLoudnessMeter(6)
    whole.push(x)
    w = whole.finalize()
    c = _measure_chunked(x, [30000, 50000, 70000, 90000])
    assert c.integrated_loudness_lufs == \
        pytest.approx(w.integrated_loudness_lufs, abs=1e-12)
