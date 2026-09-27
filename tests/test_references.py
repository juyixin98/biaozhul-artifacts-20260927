"""Cross-reference suite against three independent implementations.

1. ``independent_reference`` in this repo — hand-written energy/gating path;
2. pyloudnorm (third-party PyPI package) for integrated loudness;
3. the ffmpeg ``ebur128`` CLI filter (external binary) for I and LRA.

Known reference discrepancy (documented, not hidden): pyloudnorm 0.2.0 appends
1.5 s of silence *before* K-weighting for its LRA routine and uses 90 ms hops,
which adds filter-ringing boundary blocks; its LRA runs ~1.4 LU high on
constant/short material. ffmpeg and our kernel agree with the EBU Tech 3342
definition, so pyloudnorm is used as a tight reference ONLY for integrated
loudness (machine precision) and as a bounded sanity check for LRA.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import numpy as np
import pytest

from conftest import SR, run_measure, write_wav_bytes
from independent_reference import reference_measure

pyln = pytest.importorskip("pyloudnorm")
FFMPEG = shutil.which("ffmpeg")

ALL_FIXTURES = [
    "silence_sig", "constant_sig", "stereo_constant_sig",
    "short_burst_sig", "dynamic_sig", "dynamic_long_sig",
    "channel_change_sig",
]


# --------------------------------------------------------------------------
# Reference 1: independent in-repo implementation, block by block
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fixture_name", ALL_FIXTURES)
def test_matches_independent_reference(fixture_name, request, settings):
    sig = request.getfixturevalue(fixture_name)
    ours = run_measure(sig, settings, include_blocks=True)
    # The independent reference operates on analysis channels (LFE removed).
    analysis_samples = sig.samples
    if sig.layout == "5.1" and analysis_samples.shape[1] == 6:
        analysis_samples = np.delete(analysis_samples, 3, axis=1)
    ref = reference_measure(analysis_samples, SR, analysis_samples.shape[1])

    assert ours["integrated_loudness"]["status"] == ref.integrated_status
    assert ours["loudness_range"]["status"] == ref.lra_status

    m = ours["integrated_loudness"]
    assert m["gate_stats"]["total_blocks"] == ref.m_total
    assert m["gate_stats"]["above_absolute_gate"] == ref.m_above_abs
    assert m["gate_stats"]["above_both_gates"] == ref.m_above_both
    assert m["block_loudness_lufs"] == pytest.approx(ref.m_block_loudness, abs=1e-10)
    if ref.integrated_lufs is None:
        assert m["integrated_lufs"] is None
    else:
        assert m["integrated_lufs"] == pytest.approx(ref.integrated_lufs, abs=1e-9)

    l = ours["loudness_range"]
    assert l["gate_stats"]["total_blocks"] == ref.s_total
    assert l["gate_stats"]["above_absolute_gate"] == ref.s_above_abs
    assert l["gate_stats"]["above_both_gates"] == ref.s_above_both
    assert l["block_loudness_lufs"] == pytest.approx(ref.s_block_loudness, abs=1e-10)
    if ref.lra_lu is None:
        assert l["lra_lu"] is None
    else:
        assert l["lra_lu"] == pytest.approx(ref.lra_lu, abs=1e-9)


def test_independent_reference_disagrees_on_rms_proxy(short_burst_sig):
    """The gating math (shared by both EBU implementations) must differ from
    full-signal RMS by a wide margin for the burst fixture."""
    ref = reference_measure(short_burst_sig.samples, SR, 1)
    whole_ms = np.mean(short_burst_sig.samples ** 2)
    naive = -0.691 + 10 * np.log10(whole_ms)
    assert ref.integrated_lufs - naive > 5.0


# --------------------------------------------------------------------------
# Reference 2: pyloudnorm — integrated loudness to machine precision
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fixture_name", [
    "constant_sig", "stereo_constant_sig", "dynamic_sig",
    "dynamic_long_sig"])
def test_integrated_loudness_matches_pyloudnorm(fixture_name, request, settings):
    sig = request.getfixturevalue(fixture_name)
    ours = run_measure(sig, settings)["integrated_loudness"]["integrated_lufs"]
    theirs = float(pyln.Meter(SR).integrated_loudness(sig.samples.astype(np.float64)))
    assert ours == pytest.approx(theirs, abs=1e-9)


def test_integrated_matches_pyloudnorm_five_channel(settings):
    """5.0 path: pyloudnorm channel ordering [L,R,C,Ls,Rs] matches ours."""
    from conftest import Sig
    dur = 8.0
    t = np.arange(int(dur * SR)) / SR
    x = np.stack([
        0.4 * np.sin(2 * np.pi * 400 * t),
        0.4 * np.sin(2 * np.pi * 400 * t),
        0.3 * np.sin(2 * np.pi * 500 * t),
        0.2 * np.sin(2 * np.pi * 600 * t),
        0.2 * np.sin(2 * np.pi * 600 * t),
    ], axis=1)
    sig = Sig(x, (1.0, 1.0, 1.0, 1.41, 1.41), "5.0", 5)
    ours = run_measure(sig, settings)["integrated_loudness"]["integrated_lufs"]
    theirs = float(pyln.Meter(SR).integrated_loudness(x))
    assert ours == pytest.approx(theirs, abs=1e-9)


def test_pyloudnorm_known_lra_discrepancy_is_documented(dynamic_sig, settings):
    """Lock the documented pyloudnorm LRA offset so a future package change is
    visible, instead of silently tightening/loosening the comparison."""
    ours = run_measure(dynamic_sig, settings)["loudness_range"]["lra_lu"]
    theirs = float(pyln.Meter(SR).loudness_range(dynamic_sig.samples))
    # pyloudnorm pads with 1.5 s silence pre-filtering -> boundary tail blocks.
    assert theirs - ours == pytest.approx(1.41, abs=0.2)
    # ...and our number is the one ffmpeg/EBU gives (checked in next tests).


# --------------------------------------------------------------------------
# Reference 3: ffmpeg ebur128 CLI
# --------------------------------------------------------------------------

def _ffmpeg_ebur128(wav_path: str) -> tuple[float, float]:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", wav_path,
         "-filter_complex", "ebur128=peak=none", "-f", "null", "-"],
        capture_output=True, text=True, timeout=60)
    text = proc.stderr
    m_i = re.search(r"^\s+I:\s+(-?[\d.]+|-?inf)\s+LUFS", text, re.MULTILINE)
    m_lra = re.search(r"^\s+LRA:\s+(-?[\d.]+|-?inf)\s+LU", text, re.MULTILINE)
    assert m_i and m_lra, f"could not parse ffmpeg output:\n{text[-800:]}"
    return (float("-inf") if "inf" in m_i.group(1) else float(m_i.group(1)),
            float("-inf") if "inf" in m_lra.group(1) else float(m_lra.group(1)))


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
@pytest.mark.parametrize("fixture_name,tol", [
    ("constant_sig", 0.2),
    ("stereo_constant_sig", 0.2),
    ("dynamic_sig", 0.25),
    ("dynamic_long_sig", 0.25),
])
def test_matches_ffmpeg_ebur128(fixture_name, tol, request, settings, tmp_path):
    sig = request.getfixturevalue(fixture_name)
    ours = run_measure(sig, settings)
    wav = tmp_path / f"{fixture_name}.wav"
    wav.write_bytes(write_wav_bytes(sig.samples.astype(np.float32), SR))
    f_i, f_lra = _ffmpeg_ebur128(str(wav))
    assert ours["integrated_loudness"]["integrated_lufs"] == pytest.approx(f_i, abs=tol)
    assert ours["loudness_range"]["lra_lu"] == pytest.approx(f_lra, abs=0.5)


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
def test_ffmpeg_silence_is_at_or_below_gate_floor(silence_sig, settings, tmp_path):
    ours = run_measure(silence_sig, settings)
    assert ours["status"] == "SILENCE"
    wav = tmp_path / "silence.wav"
    wav.write_bytes(write_wav_bytes(silence_sig.samples.astype(np.float32), SR))
    f_i, f_lra = _ffmpeg_ebur128(str(wav))
    # This ffmpeg build clamps a silent program to the gate floor rather than
    # printing -inf; either output proves nothing passed the gates.
    assert f_i <= -70.0
    assert f_lra == 0.0


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
def test_ffmpeg_multichannel_5point1(channel_change_sig, settings, tmp_path):
    """ffmpeg's own LFE handling vs our explicit channel selection."""
    ours = run_measure(channel_change_sig, settings)
    wav = tmp_path / "fiveone.wav"
    wav.write_bytes(write_wav_bytes(channel_change_sig.samples.astype(np.float32), SR))
    f_i, _ = _ffmpeg_ebur128(str(wav))
    assert ours["integrated_loudness"]["integrated_lufs"] == pytest.approx(f_i, abs=0.6)
