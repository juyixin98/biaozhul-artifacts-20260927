"""Standalone synthetic fixture generator.

Independent of the clockalign *core*: imports none of ``timefit``, ``pulses``,
``correlation``, ``resample`` or ``pipeline``. The ground truth written to
``*.truth.json`` is computed directly from the construction parameters, so
recovery tests judge the package against truth the package itself never
produced.

Scenario physics
----------------
Reality timeline ``t``. Both devices capture the same acoustic field.

* reference recorder is honest: ``tA = t``;
* slave clock runs at ``slope = 1 + ppm*1e-6`` and starts at ``offset_s``:
  reality content at ``t`` is stamped ``tB = slope*t + offset``;
* dropped frames are real *splices*: a contiguous run of slave samples is
  deleted and the tail is pulled forward, creating a discontinuity;
* ``bad_sync`` adds extra pulses to the slave track only -- false sync points
  the robust fitter must reject.

Each scenario writes ``<name>.wav`` (ch0 reference, ch1 slave) and
``<name>.truth.json``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .media_util import (add_burst_at, set_noise_master,
                         shared_content, write_stereo_wav)

SAMPLE_RATE = 16000
PULSE_HZ = 1000.0
PULSE_DURATION_S = 0.02
PULSE_TIMES_S = (2.0, 3.5, 5.0, 6.5, 8.0, 9.5, 11.0)
DROP_SPECS = ((7.0, 160),)  # (reality time of cut, removed slave samples)
DURATION_S = 12.0
SEED = 20260927


@dataclass(frozen=True)
class Scenario:
    name: str
    drift_ppm: float
    offset_s: float
    drops: tuple[tuple[float, int], ...]
    spurious_slave_pulses: tuple[float, ...]
    pulse_times: tuple[float, ...]
    correlated_content: bool
    duration_s: float
    description: str


SCENARIOS: dict[str, Scenario] = {
    "drift_offset": Scenario(
        name="drift_offset", drift_ppm=120.0, offset_s=0.25,
        drops=(), spurious_slave_pulses=(), pulse_times=PULSE_TIMES_S,
        correlated_content=True, duration_s=DURATION_S,
        description="120 ppm drift plus 250 ms fixed offset; no frame loss"),
    "dropped_frames": Scenario(
        name="dropped_frames", drift_ppm=80.0, offset_s=-0.12,
        drops=DROP_SPECS, spurious_slave_pulses=(),
        pulse_times=PULSE_TIMES_S, correlated_content=True,
        duration_s=DURATION_S,
        description="80 ppm drift, -120 ms offset, one 10 ms dropped block"),
    "bad_sync": Scenario(
        name="bad_sync", drift_ppm=150.0, offset_s=0.18,
        drops=(), spurious_slave_pulses=(4.25, 8.75),
        pulse_times=PULSE_TIMES_S, correlated_content=True,
        duration_s=DURATION_S,
        description="extra slave-only pulses that are false sync points"),
    "correlation": Scenario(
        name="correlation", drift_ppm=95.0, offset_s=0.2,
        drops=(), spurious_slave_pulses=(), pulse_times=(),
        correlated_content=True, duration_s=DURATION_S,
        description="no pulses; same content must be aligned by correlation"),
    "insufficient_sync": Scenario(
        name="insufficient_sync", drift_ppm=60.0, offset_s=0.05,
        drops=(), spurious_slave_pulses=(), pulse_times=(2.0, 9.0),
        correlated_content=False, duration_s=DURATION_S,
        description="two pulses over uncorrelated noise: not enough evidence"),
}


def _grid(fs: int, duration_s: float) -> np.ndarray:
    n = int(round(duration_s * fs))
    return (np.arange(n) + 0.5) / fs


def _render(sc: Scenario, fs: int, *, slave: bool) -> np.ndarray:
    # Uncorrelated content uses different, fixed-but-distinct streams on the
    # two devices so there really is no shared waveform to lock onto.
    rng = np.random.Generator(np.random.PCG64(
        SEED ^ (0x5151 if slave else 0xA17C)))
    t_self = _grid(fs, sc.duration_s)
    if slave:
        slope = 1.0 + sc.drift_ppm * 1e-6
        reality = (t_self - sc.offset_s) / slope
        field = shared_content(reality, rng, correlated=sc.correlated_content)
        # Real pulses happen at reality times; stamp them at slave clock time.
        for p in sc.pulse_times:
            tb_p = slope * p + sc.offset_s
            field = add_burst_at(field, t_self, tb_p, PULSE_HZ,
                                 PULSE_DURATION_S, fs, amplitude=0.6)
        # False pulses exist only on the slave device, stamped at slave time.
        for q in sc.spurious_slave_pulses:
            field = add_burst_at(field, t_self, q, PULSE_HZ,
                                 PULSE_DURATION_S, fs, amplitude=0.6)
        return field.astype(np.float32)
    field = shared_content(t_self, rng, correlated=sc.correlated_content)
    for p in sc.pulse_times:
        field = add_burst_at(field, t_self, p, PULSE_HZ, PULSE_DURATION_S,
                             fs, amplitude=0.6)
    return field.astype(np.float32)


def _apply_drops(slave_samples: np.ndarray, sc: Scenario, fs: int
                 ) -> list[dict]:
    slope = 1.0 + sc.drift_ppm * 1e-6
    records: list[dict] = []
    samples = slave_samples
    # Splice from the last cut backwards so earlier indices stay valid.
    for t_real, drop_len in sorted(sc.drops, key=lambda d: d[0], reverse=True):
        t_cut_slave = slope * t_real + sc.offset_s
        start = int(round(t_cut_slave * fs))
        n = samples.size
        if 0 < start < n - drop_len:
            samples = np.concatenate([
                samples[:start], samples[start + drop_len:],
                np.zeros(drop_len, dtype=samples.dtype)])
            records.append({
                "reality_time_s": float(t_real),
                "slave_time_before_splice_s": float(t_cut_slave),
                "removed_samples": int(drop_len),
                "removed_duration_s": float(drop_len / fs)})
    slave_samples[:] = samples
    return records


def generate(out_dir: str | Path, fs: int = SAMPLE_RATE,
             names: tuple[str, ...] | None = None) -> dict[str, dict]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    n = int(round(DURATION_S * fs))
    index: dict[str, dict] = {}
    for name in (names or tuple(SCENARIOS)):
        sc = SCENARIOS[name]
        # One shared reality-noise field per scenario, drawn independently of
        # the core; both renderers resample this same array.
        master_rng = np.random.Generator(np.random.PCG64(SEED + hash_jump(name)))
        set_noise_master(master_rng.standard_normal(n).astype(np.float32), fs)
        ref = _render(sc, fs, slave=False)
        slv = _render(sc, fs, slave=True)
        drops = _apply_drops(slv, sc, fs)
        write_stereo_wav(out / f"{name}.wav", ref, slv, fs)
        truth = {
            "scenario": sc.name,
            "description": sc.description,
            "sample_rate_hz": fs,
            "drift_ppm": sc.drift_ppm,
            "offset_s": sc.offset_s,
            "slope": 1.0 + sc.drift_ppm * 1e-6,
            "pulse_times_reality_s": list(sc.pulse_times),
            "pulse_slave_times_s": [
                (1.0 + sc.drift_ppm * 1e-6) * p + sc.offset_s
                for p in sc.pulse_times],
            "spurious_slave_pulse_times_s": list(sc.spurious_slave_pulses),
            "drop_times_s": [d["reality_time_s"] for d in drops],
            "drops": drops,
            "pulse_frequency_hz": PULSE_HZ,
            "pulse_duration_s": PULSE_DURATION_S,
            "channels": {"0": "reference", "1": "slave"},
            "generated_by": "tools.fixturegen (independent; no core imports)",
        }
        with open(out / f"{name}.truth.json", "w", encoding="utf-8") as fh:
            json.dump(truth, fh, indent=2, sort_keys=True)
        index[name] = {"wav": str(out / f"{name}.wav"),
                       "truth": str(out / f"{name}.truth.json")}
    with open(out / "index.json", "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2, sort_keys=True)
    return index


def hash_jump(name: str) -> int:
    return sum((k + 1) * ord(c) for k, c in enumerate(name)) % 100000
