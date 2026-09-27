"""Synthetic fixtures with known ground truth.

The underlying content is an *analytic* function of time (a fixed sum of
sinusoidal partials plus linear chirps). The reference and target recordings
are produced by evaluating that function on two different time grids — no
resampling code from the core under test is used here, so fixture ground
truth stays an independent reference.

Target clock model: a pulse occurring at reference time r appears in the
target recording at target time  t = offset_s + (1 + drift_ppm*1e-6) * r.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Content partials: (frequency_hz, amplitude). Fixed constants — the same
# analytic signal underlies every fixture, only the time grids differ.
_PARTIALS: tuple[tuple[float, float], ...] = (
    (110.0, 0.20), (173.0, 0.12), (251.0, 0.10), (327.0, 0.06),
    (419.0, 0.08), (563.0, 0.05), (701.0, 0.04), (887.0, 0.03),
)
_PHASES: tuple[float, ...] = (0.0, 1.1, 2.3, 0.7, 2.9, 1.7, 0.4, 2.1)


def chirp(t: np.ndarray, f0_hz: float, f1_hz: float, duration_s: float) -> np.ndarray:
    """Hann-windowed linear chirp; zero outside [0, duration_s]."""
    out = np.zeros_like(t)
    m = (t >= 0.0) & (t <= duration_s)
    tc = t[m]
    phase = 2.0 * np.pi * (f0_hz * tc + (f1_hz - f0_hz) / (2.0 * duration_s) * tc**2)
    window = 0.5 - 0.5 * np.cos(2.0 * np.pi * tc / duration_s)
    out[m] = window * np.sin(phase)
    return out


def content_signal(t: np.ndarray) -> np.ndarray:
    """Deterministic band-limited 'programme material'."""
    out = np.zeros_like(t)
    for (f, a), ph in zip(_PARTIALS, _PHASES):
        out += a * np.sin(2.0 * np.pi * f * t + ph)
    return out


def pulse_train(t: np.ndarray, pulse_times_s: list[float], duration_s: float,
                f0_hz: float, f1_hz: float, amplitude: float) -> np.ndarray:
    out = np.zeros_like(t)
    for p in pulse_times_s:
        out += amplitude * chirp(t - p, f0_hz, f1_hz, duration_s)
    return out


@dataclass(frozen=True)
class DropSpec:
    at_target_time_s: float
    duration_s: float


@dataclass(frozen=True)
class SpuriousPulse:
    at_target_time_s: float
    amplitude: float = 1.3


@dataclass(frozen=True)
class FixtureSpec:
    fs: int = 8000
    duration_s: float = 8.0
    pulse_times_s: tuple[float, ...] = (0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5)
    pulse_duration_s: float = 0.08
    pulse_f0_hz: float = 1200.0
    pulse_f1_hz: float = 3200.0
    pulse_amplitude: float = 0.9
    offset_s: float = 0.123
    drift_ppm: float = 75.0
    noise_rms: float = 0.004
    drops: tuple[DropSpec, ...] = ()
    spurious: tuple[SpuriousPulse, ...] = ()
    seed: int = 20260927


@dataclass
class FixtureResult:
    reference: np.ndarray
    target: np.ndarray
    fs: int
    ground_truth: dict = field(default_factory=dict)


def generate_fixture(spec: FixtureSpec) -> FixtureResult:
    """Generate (reference, target) with fully known ground truth."""
    fs = spec.fs
    rng = np.random.default_rng(spec.seed)
    pulse_times = list(spec.pulse_times_s)

    # Reference: sample the analytic content on the ideal grid.
    n_ref = int(spec.duration_s * fs)
    t_ref = np.arange(n_ref) / fs
    reference = (
        content_signal(t_ref)
        + pulse_train(t_ref, pulse_times, spec.pulse_duration_s,
                      spec.pulse_f0_hz, spec.pulse_f1_hz, spec.pulse_amplitude)
        + rng.normal(0.0, spec.noise_rms, n_ref)
    )

    # Target: same content evaluated on the drifted/offset grid.
    drift = spec.drift_ppm * 1e-6
    n_tgt = int(spec.duration_s * (1.0 + drift) * fs) + int(spec.offset_s * fs) + fs // 4
    t_tgt = np.arange(n_tgt) / fs
    t_ref_equiv = (t_tgt - spec.offset_s) / (1.0 + drift)
    target = (
        content_signal(t_ref_equiv)
        + pulse_train(t_ref_equiv, pulse_times, spec.pulse_duration_s,
                      spec.pulse_f0_hz, spec.pulse_f1_hz, spec.pulse_amplitude)
        + rng.normal(0.0, spec.noise_rms, n_tgt)
    )

    # Spurious (wrong) sync points: chirps at times where no real pulse sits.
    for sp in spec.spurious:
        target += sp.amplitude * chirp(
            t_tgt - sp.at_target_time_s,
            spec.pulse_f0_hz, spec.pulse_f1_hz, spec.pulse_duration_s,
        )

    # Dropped frames: contiguous samples simply go missing; later audio
    # shifts earlier on the target timeline by the dropped duration.
    drop_mask = np.ones(n_tgt, dtype=bool)
    for d in spec.drops:
        i0 = int(d.at_target_time_s * fs)
        i1 = i0 + int(d.duration_s * fs)
        drop_mask[i0:i1] = False
    target = target[drop_mask]

    ground_truth = {
        "offset_s": spec.offset_s,
        "drift_ppm": spec.drift_ppm,
        "pulse_times_s": pulse_times,
        "drops": [{"at_target_time_s": d.at_target_time_s,
                   "duration_s": d.duration_s} for d in spec.drops],
        "spurious_pulses": [{"at_target_time_s": s.at_target_time_s,
                             "amplitude": s.amplitude} for s in spec.spurious],
        "note": (
            "ground truth of the synthesis parameters; correlation-based "
            "estimates are checked against these, never treated as absolute "
            "time proof"
        ),
    }
    return FixtureResult(reference=reference, target=target, fs=fs,
                         ground_truth=ground_truth)


def reference_metadata(spec: FixtureSpec) -> dict:
    return {
        "pulses": {
            "times_s": list(spec.pulse_times_s),
            "duration_s": spec.pulse_duration_s,
            "f0_hz": spec.pulse_f0_hz,
            "f1_hz": spec.pulse_f1_hz,
        },
        "events": [],
    }


def target_metadata(spec: FixtureSpec) -> dict:
    """Events on the *target* clock, for the metadata-mapping output."""
    drift = spec.drift_ppm * 1e-6

    def to_target(t_ref: float) -> float:
        return spec.offset_s + (1.0 + drift) * t_ref

    return {
        "pulses": None,
        "events": [
            {"name": "segment_start", "time_s": round(to_target(1.0), 6)},
            {"name": "annotation_a", "time_s": round(to_target(3.0), 6)},
            {"name": "segment_end", "time_s": round(to_target(7.0), 6)},
        ],
    }
