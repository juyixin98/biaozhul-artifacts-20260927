"""End-to-end parameter recovery against independently generated fixtures.

These are the core acceptance tests. The truth comes from
``tools/fixturegen`` (a standalone generator that imports no package core);
each test asserts *concrete* recovered values, alignment residuals and, where
appropriate, the specific failure category -- not just "the API answered".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from fixturegen import generate  # noqa: E402

from clockalign.media import load_track  # noqa: E402
from clockalign.pipeline import run_alignment  # noqa: E402
from clockalign.validation import validate_report  # noqa: E402


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    out = tmp_path_factory.mktemp("fixtures")
    generate(out)
    return out


def _run(cfg, fixtures, name, **kw):
    wav = fixtures / f"{name}.wav"
    ref = load_track(wav, 0)
    slv = load_track(wav, 1)
    return run_alignment(ref, slv, cfg, **kw)


def _truth(fixtures, name):
    return json.loads((fixtures / f"{name}.truth.json").read_text())


def test_recovers_known_drift_and_offset(cfg, fixtures):
    truth = _truth(fixtures, "drift_offset")
    result = _run(cfg, fixtures, "drift_offset")
    assert result.status == "corrected"
    ppm = result.estimate["drift_ppm"]
    off = result.estimate["offset_s"]
    assert abs(ppm - truth["drift_ppm"]) <= cfg.validation.drift_ppm_tol
    assert abs(off - truth["offset_s"]) * 1000 <= cfg.validation.offset_ms_tol
    # Residual and usable interval are reported and bounded.
    rr = result.residual_report
    assert rr["global"]["rms_residual_s"] <= cfg.validation.residual_rms_max_s
    usable = rr["usable_reference_range_s"]
    assert usable[1] - usable[0] > 8.0


def test_drift_estimate_requires_more_than_first_timestamp(cfg, fixtures):
    # If the estimator had merely subtracted the first timestamp it could not
    # recover the slope; assert a non-trivial drift is actually found and that
    # several sync points were used.
    result = _run(cfg, fixtures, "drift_offset")
    assert result.estimate["n_sync_points_used"] >= 4
    assert abs(result.estimate["drift_ppm"] - 120.0) <= 15.0


def test_recovers_parameters_with_dropped_frame_and_localizes_it(cfg, fixtures):
    truth = _truth(fixtures, "dropped_frames")
    result = _run(cfg, fixtures, "dropped_frames")
    assert result.status == "corrected"
    assert abs(result.estimate["drift_ppm"] - truth["drift_ppm"]) <= \
        cfg.validation.drift_ppm_tol
    assert abs(result.estimate["offset_s"] - truth["offset_s"]) * 1000 <= \
        cfg.validation.offset_ms_tol
    # Two independently valid segments around the splice.
    segs = result.residual_report["segments"]
    assert len(segs) == 2
    for s in segs:
        assert s["rms_residual_s"] <= cfg.validation.residual_rms_max_s
    # The frame drop is localized to the known cut within tolerance.
    drops = [g for g in result.residual_report["frame_gaps"]
             if g["kind"] == "frame_drop"]
    assert len(drops) == 1
    assert abs(drops[0]["reference_time_start_s"] - truth["drop_times_s"][0]) \
        <= cfg.validation.drop_time_tol_s
    assert drops[0]["estimated_missing_slave_samples"] == pytest.approx(
        160, abs=10)


def test_false_sync_points_are_rejected_not_fit(cfg, fixtures):
    truth = _truth(fixtures, "bad_sync")
    result = _run(cfg, fixtures, "bad_sync")
    assert result.status == "corrected"
    assert abs(result.estimate["drift_ppm"] - truth["drift_ppm"]) <= \
        cfg.validation.drift_ppm_tol
    # The two injected slave-only pulses are surfaced, not silently used.
    unmatched = result.sync_points["unmatched_slave_pulses_s"]
    for q in truth["spurious_slave_pulse_times_s"]:
        assert any(abs(u - q) < 0.05 for u in unmatched), unmatched


def test_correlation_peak_is_not_claimed_as_absolute_time(cfg, fixtures):
    result = _run(cfg, fixtures, "correlation")
    assert result.status == "corrected"
    # Used content correlation, not pulses.
    assert result.mode_used in ("correlate", "auto")
    # Relative-only caveat is present and the anchor flag is false.
    assert result.external_time_anchor is False
    codes = [u["code"] for u in result.uncertainties]
    assert "relative_time_only" in codes
    assert "absolute" in result.timeline_map["time_basis"].lower()


def test_correlation_mode_recovers_drift(cfg, fixtures):
    truth = _truth(fixtures, "correlation")
    result = _run(cfg, fixtures, "correlation", mode="correlate")
    assert result.status == "corrected"
    assert abs(result.estimate["drift_ppm"] - truth["drift_ppm"]) <= \
        cfg.validation.drift_ppm_tol
    assert result.residual_report["global"]["rms_residual_s"] <= \
        cfg.validation.residual_rms_max_s


@pytest.mark.parametrize("scenario",
                         ["drift_offset", "dropped_frames", "bad_sync",
                          "correlation"])
def test_corrected_audio_aligns_at_zero_lag(cfg, fixtures, scenario):
    # Independent, waveform-level check: inside the usable interval the
    # corrected slave must correlate with the reference best at zero lag.
    result = _run(cfg, fixtures, scenario)
    assert result.status == "corrected"
    ref = load_track(fixtures / f"{scenario}.wav", 0)
    corr = result.correction.corrected_audio.astype(np.float64)
    lo, hi = result.residual_report["usable_reference_range_s"]
    fs = ref.sample_rate
    i0, i1 = int((lo + 0.2) * fs), int((hi - 0.2) * fs)
    x = ref.samples[i0:i1].astype(np.float64)
    y = corr[i0:i1]
    assert y.size == x.size and y.size > 0

    def ncc_lag(lag):
        if lag >= 0:
            a, b = x[: x.size - lag], y[lag:]
        else:
            a, b = x[-lag:], y[: y.size + lag]
        return float(np.sum(a * b) / np.sqrt(np.sum(a * a) * np.sum(b * b)))

    ncc0 = ncc_lag(0)
    assert ncc0 > 0.8, f"zero-lag alignment NCC only {ncc0:.3f}"
    # No non-zero shift within +/-3 ms may correlate better: the warp really
    # removed the drift, not just roughly.
    span = int(0.003 * fs)
    others = [ncc_lag(d) for d in range(-span, span + 1) if d != 0]
    assert ncc0 >= max(others) - 1e-3


def test_insufficient_evidence_is_not_corrected(cfg, fixtures):
    result = _run(cfg, fixtures, "insufficient_sync")
    assert result.status == "uncorrected"
    assert result.failure["code"] == "insufficient_evidence"
    assert result.correction is None
    assert result.timeline_map is None
    assert result.residual_report is None
    # The reason is human-readable and specific.
    assert "sync point" in result.failure["message"]


def test_validate_report_matches_independent_truth(cfg, fixtures):
    truth = _truth(fixtures, "drift_offset")
    result = _run(cfg, fixtures, "drift_offset")
    verdict = validate_report(result.to_dict(), cfg.validation, truth={
        "drift_ppm": truth["drift_ppm"],
        "offset_s": truth["offset_s"],
        "drop_times_s": truth["drop_times_s"],
    })
    assert verdict["passed"], verdict["failure_categories"]
    assert verdict["failure_categories"] == []


def test_validate_report_flags_wrong_truth(cfg, fixtures):
    result = _run(cfg, fixtures, "drift_offset")
    verdict = validate_report(result.to_dict(), cfg.validation, truth={
        "drift_ppm": 999.0, "offset_s": -0.5})
    assert not verdict["passed"]
    assert "drift_out_of_tolerance" in verdict["failure_categories"]
    assert "offset_out_of_tolerance" in verdict["failure_categories"]
