"""Unit tests for pulse detection and pairing."""
from __future__ import annotations

import numpy as np

from clockalign.pulses import (Pulse, detect_pulses, pair_pulses,
                               pulse_template)

FS = 16000


def _track_with_pulses(times, *, fs=FS, amp=0.6):
    n = int(12 * fs)
    sig = np.zeros(n, dtype=np.float64)
    tmpl = pulse_template(1000.0, 0.02, fs)
    rng = np.random.Generator(np.random.PCG64(3))
    sig += 0.02 * rng.standard_normal(n)
    for t in times:
        i = int(round(t * fs))
        end = min(n, i + tmpl.size)
        sig[i:end] += amp * tmpl[: end - i]
    return sig.astype(np.float32), tmpl


def test_detects_all_pulses_at_true_times():
    times = [2.0, 3.5, 5.0, 6.5, 8.0]
    sig, tmpl = _track_with_pulses(times)
    pulses = detect_pulses(sig, FS, tmpl, score_threshold=0.4,
                           min_spacing_s=0.1)
    assert len(pulses) == len(times)
    for p, t in zip(pulses, times):
        # sub-millisecond onset accuracy
        assert abs(p.time_s - t) < 1e-3, (p.time_s, t)
        assert 0.0 <= p.score <= 1.0


def test_no_pulses_in_silence():
    tmpl = pulse_template(1000.0, 0.02, FS)
    sig = (0.001 * np.random.default_rng(0).standard_normal(FS * 4)
           ).astype(np.float32)
    assert detect_pulses(sig, FS, tmpl, score_threshold=0.4) == []


def test_pairing_is_order_preserving_and_one_to_one():
    a = [Pulse(time_s=t, score=0.9, sample=t * FS)
         for t in (2.0, 3.5, 5.0, 6.5)]
    b = [Pulse(time_s=t + 0.25, score=0.9, sample=(t + 0.25) * FS)
         for t in (2.0, 3.5, 5.0, 6.5)]
    pairs, un_a, un_b = pair_pulses(a, b, max_offset_s=1.0)
    assert len(pairs) == 4
    assert un_a == [] and un_b == []
    assert [p.t_a for p in pairs] == sorted(p.t_a for p in pairs)
    assert [p.t_b for p in pairs] == sorted(p.t_b for p in pairs)


def test_pairing_excludes_device_only_false_pulse():
    a = [Pulse(time_s=t, score=0.9, sample=t * FS)
         for t in (2.0, 4.0, 6.0, 8.0)]
    # One extra pulse at 5.1 exists only on B (false sync point).
    b_times = [2.25, 4.25, 5.1, 6.25, 8.25]
    b = [Pulse(time_s=t, score=0.9, sample=t * FS) for t in b_times]
    pairs, _, un_b = pair_pulses(a, b, max_offset_s=1.0)
    assert len(pairs) == 4
    assert 5.1 in [round(x, 3) for x in un_b]
    # No real A pulse got paired to the false pulse.
    assert all(abs(p.t_b - (p.t_a + 0.25)) < 0.01 for p in pairs)


def test_pairing_survives_frame_drop_jump():
    # B mapping is t+0.1 before t=6 and t+0.1-0.02 after a 20 ms splice.
    a = [Pulse(time_s=t, score=0.9, sample=t * FS)
         for t in (2.0, 3.5, 5.0, 6.5, 8.0, 9.5)]
    b_times = [t + 0.1 - (0.02 if t > 6 else 0.0) for t in
               (2.0, 3.5, 5.0, 6.5, 8.0, 9.5)]
    b = [Pulse(time_s=t, score=0.9, sample=t * FS) for t in b_times]
    pairs, un_a, un_b = pair_pulses(a, b, max_offset_s=1.0)
    assert len(pairs) == 6
    assert un_a == [] and un_b == []
