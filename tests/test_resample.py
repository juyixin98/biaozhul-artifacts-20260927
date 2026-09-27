"""Unit tests for resampling correction and the separate timeline metadata."""
from __future__ import annotations

import numpy as np
import pytest

from clockalign.discontinuity import Cut
from clockalign.resample import correct_track
from clockalign.timefit import (AffineModel, FitResult, Segment, SyncPoint)

FS = 16000


def _one_segment_fit(ppm, offset, t0, t1, n=5):
    ta = np.linspace(t0, t1, n)
    tb = (1 + ppm * 1e-6) * ta + offset
    pts = [SyncPoint(t_a=float(a), t_b=float(b)) for a, b in zip(ta, tb)]
    model = AffineModel(slope=1 + ppm * 1e-6, intercept=offset)
    seg = Segment(index=0, t_a_start=float(ta[0]), t_a_end=float(ta[-1]),
                  model=model, rms_residual_s=0.0, max_abs_residual_s=0.0,
                  n_points=n, inlier_t_a=list(ta), inlier_t_b=list(tb),
                  residual_s=[0.0] * n)
    return FitResult(status="ok", global_model=model, drift_ppm=float(ppm),
                     offset_s=float(offset), segments=(seg,),
                     inlier_points=tuple(pts), outlier_points=(),
                     rms_residual_s=0.0, max_abs_residual_s=0.0,
                     usable_t_a_range=(float(ta[0]), float(ta[-1])))


def test_resampled_audio_aligns_with_reference_grid():
    # Slave is a pure 440 Hz tone; after correction it must line up, sample
    # for sample, with the same tone placed on the reference timeline.
    fs = FS
    n = int(4 * fs)
    ppm, offset = 100.0, 0.02
    t_ref = np.arange(n) / fs
    reference = np.sin(2 * np.pi * 440 * t_ref).astype(np.float32)
    # Slave recording of that tone under its clock: sampled at t_b mapping.
    t_b = np.arange(n) / fs
    reality = (t_b - offset) / (1 + ppm * 1e-6)
    slave = np.sin(2 * np.pi * 440 * reality).astype(np.float32)

    fit = _one_segment_fit(ppm, offset, 0.5, 3.5)
    corr = correct_track(slave, fs, fit, reference_length=n,
                         edge_guard_s=0.01)
    assert corr.corrected_audio.shape == reference.shape
    mid = slice(int(1.0 * fs), int(3.0 * fs))
    err = np.mean(np.abs(corr.corrected_audio[mid] - reference[mid]))
    assert err < 0.02


def test_timeline_map_is_separate_from_audio_and_documents_mapping():
    fit = _one_segment_fit(120.0, 0.25, 2.0, 10.0)
    corr = correct_track(np.zeros(FS, dtype=np.float32), FS, fit,
                         reference_length=FS)
    tmap = corr.timeline_map
    assert tmap["global_model"]["drift_ppm"] == pytest.approx(120.0)
    assert tmap["global_model"]["intercept_s"] == pytest.approx(0.25)
    assert "t_slave = slope * t_reference + intercept" in \
        tmap["segments"][0]["mapping"]["model"]
    # Epistemic boundary is stated in the data, not just the docs.
    assert "absolute" in tmap["time_basis"]


def test_dropped_frame_emits_explicit_zero_gap():
    fs = FS
    n = int(4 * fs)
    t0, t1, t2, t3 = 0.5, 1.5, 2.5, 3.5
    m0 = AffineModel(slope=1.0, intercept=0.0)
    m1 = AffineModel(slope=1.0, intercept=-0.05)  # 50 ms splice
    s0 = Segment(0, t0, t1, m0, 0, 0, 3, [t0, 1.0, t1],
                 [t0, 1.0, t1], [0, 0, 0])
    s1 = Segment(1, t2, t3, m1, 0, 0, 3, [t2, 3.0, t3],
                 [t2 - 0.05, 3.0 - 0.05, t3 - 0.05], [0, 0, 0])
    fit = FitResult("ok", m0, 0.0, 0.0, (s0, s1), (), (), 0, 0, (t0, t3))
    cut = Cut(t_a=2.0, jump_b=-0.05, kind="frame_drop",
              localization="content_scan", scan_score_margin=0.9)
    corr = correct_track(np.zeros(n, dtype=np.float32), fs, fit,
                         reference_length=n, cuts=[cut])
    drop_gaps = [g for g in corr.gaps if g.kind == "frame_drop"]
    assert len(drop_gaps) == 1
    gap = drop_gaps[0]
    assert gap.t_a_start == pytest.approx(2.0, abs=0.01)
    assert gap.t_a_end - gap.t_a_start == pytest.approx(0.05, abs=0.005)
    # The corrected audio actually contains a zeroed hole at the gap.
    i = int(2.02 * fs)
    assert corr.corrected_audio[i] == 0.0
    # The cut localization method is recorded in the metadata.
    assert corr.timeline_map["cuts"][0]["localization"] == "content_scan"
