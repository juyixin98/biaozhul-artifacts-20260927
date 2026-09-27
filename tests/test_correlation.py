"""Unit tests for content-based correlation sync points."""
from __future__ import annotations

import numpy as np

from clockalign.correlation import find_correlation_points

FS = 16000


def _shared_tracks(ppm, offset_s, *, noise=0.1, seed=5, duration=6.0):
    n = int(duration * FS)
    rng = np.random.default_rng(seed)
    t = np.arange(n) / FS
    content = (0.4 * np.sin(2 * np.pi * 250 * t)
               + noise * rng.standard_normal(n))
    a = content.astype(np.float32)
    # Slave: same reality content played under a shifted/stretched clock.
    t_b = np.arange(n) / FS
    reality = (t_b - offset_s) / (1 + ppm * 1e-6)
    pos = reality * FS - 0.5
    i0 = np.floor(pos).astype(np.int64)
    f = pos - i0
    valid = (i0 >= 0) & (i0 + 1 < n)
    b = np.zeros(n, dtype=np.float32)
    b[valid] = (content[i0[valid]] * (1 - f[valid])
                + content[np.clip(i0[valid] + 1, 0, n - 1)] * f[valid])
    return a, b


def test_finds_drifting_offset_line():
    ppm, offset = 90.0, 0.15
    a, b = _shared_tracks(ppm, offset)
    pts = find_correlation_points(
        a, b, FS, window_s=0.5, hop_s=1.0, search_half_window_s=0.4,
        min_score=0.5)
    assert len(pts) >= 4
    offs = np.array([p.t_b - p.t_a for p in pts])
    ta = np.array([p.t_a for p in pts])
    slope, intercept = np.polyfit(ta, offs, 1)
    assert abs(intercept - offset) < 0.005
    assert abs(slope * 1e6 - ppm) < 25.0
    assert all(p.score >= 0.5 for p in pts)


def test_uncorrelated_noise_yields_no_points():
    n = int(6 * FS)
    rng = np.random.default_rng(2)
    a = (0.3 * rng.standard_normal(n)).astype(np.float32)
    b = (0.3 * np.random.default_rng(3).standard_normal(n)).astype(np.float32)
    pts = find_correlation_points(
        a, b, FS, window_s=0.5, hop_s=1.0, search_half_window_s=0.3,
        min_score=0.7)
    assert pts == []
