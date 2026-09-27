"""Chunked replay must reproduce whole-clip statistics block-by-block.

This is the central streaming contract: the same PCM fed in arbitrary chunk
sizes (including odd sizes that misalign with 400 ms / 3 s windows and
100 ms hops) must give identical blocks, gate counts and final LUFS/LRA.
"""

from __future__ import annotations

import numpy as np
import pytest

from conftest import SR, run_measure

CHUNK_SIZES = [1, 7, 100, 4800, 4096, 47999, 200000]


@pytest.mark.parametrize("chunk", CHUNK_SIZES)
def test_whole_vs_chunked_momentary_blocks(constant_sig, settings, chunk):
    whole = run_measure(constant_sig, settings, include_blocks=True)
    streamed = run_measure(constant_sig, settings, include_blocks=True,
                           chunk_samples=chunk)
    assert streamed["integrated_loudness"]["block_loudness_lufs"] == \
        whole["integrated_loudness"]["block_loudness_lufs"]
    assert streamed["integrated_loudness"]["gate_stats"] == \
        whole["integrated_loudness"]["gate_stats"]
    assert streamed["integrated_loudness"]["integrated_lufs"] == pytest.approx(
        whole["integrated_loudness"]["integrated_lufs"])


@pytest.mark.parametrize("chunk", CHUNK_SIZES)
def test_whole_vs_chunked_shortterm_and_lra(dynamic_sig, settings, chunk):
    whole = run_measure(dynamic_sig, settings, include_blocks=True)
    streamed = run_measure(dynamic_sig, settings, include_blocks=True,
                           chunk_samples=chunk)
    assert streamed["loudness_range"]["block_loudness_lufs"] == \
        whole["loudness_range"]["block_loudness_lufs"]
    assert streamed["loudness_range"]["lra_lu"] == pytest.approx(
        whole["loudness_range"]["lra_lu"], abs=1e-12)
    assert streamed["loudness_range"]["gate_stats"] == \
        whole["loudness_range"]["gate_stats"]


def test_chunking_preserves_silence_and_burst_statuses(
        silence_sig, short_burst_sig, settings):
    for sig_fixture in (silence_sig, short_burst_sig):
        whole = run_measure(sig_fixture, settings)
        streamed = run_measure(sig_fixture, settings, chunk_samples=3333)
        assert streamed["status"] == whole["status"]
        assert (streamed["integrated_loudness"]["integrated_lufs"]
                == whole["integrated_loudness"]["integrated_lufs"])
        assert streamed["integrated_loudness"]["gate_stats"] == \
            whole["integrated_loudness"]["gate_stats"]


def test_two_different_chunkings_are_identical(channel_change_sig, settings):
    a = run_measure(channel_change_sig, settings, include_blocks=True, chunk_samples=512)
    b = run_measure(channel_change_sig, settings, include_blocks=True, chunk_samples=99991)
    assert a["integrated_loudness"]["block_loudness_lufs"] == \
        b["integrated_loudness"]["block_loudness_lufs"]
    assert a["loudness_range"]["block_loudness_lufs"] == \
        b["loudness_range"]["block_loudness_lufs"]
    assert a["status"] == b["status"]
