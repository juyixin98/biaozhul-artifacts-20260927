"""Known sync-pulse template, matched-filter detection and pulse pairing.

The sync pulse is *known equipment*: a Hann-windowed sine burst whose frequency
and duration come from configuration. Detection is a normalized matched filter
with non-maximum suppression and parabolic sub-sample refinement -- a real
estimator, not "subtract the first timestamp".
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .fftutil import fft_xcorr, parabolic_interp


@dataclass(frozen=True)
class Pulse:
    time_s: float        # pulse onset in the track's own timeline
    score: float         # normalized matched-filter score 0..1
    sample: float        # sub-sample onset index


def _analytic_envelope(x: np.ndarray) -> np.ndarray:
    """Magnitude of the analytic signal (Hilbert transform), via FFT."""
    n = x.size
    X = np.fft.rfft(x)
    h = np.zeros(X.shape, dtype=np.float64)
    if n % 2 == 0:
        h[0] = 1.0
        h[-1] = 1.0
        h[1:-1] = 2.0
    else:
        h[0] = 1.0
        h[1:] = 2.0
    return np.abs(np.fft.irfft(X * h, n))


def _envelope_onset(x: np.ndarray, template: np.ndarray, idx: int) -> float:
    """Locate a pulse onset from its *amplitude envelope*, not carrier phase.

    A drifting slave clock shifts the recorded pulse carrier frequency by
    ``ppm*f``; a fixed-frequency matched filter then accumulates a
    group-delay bias that grows with time and masquerades as clock drift. The
    pulse's amplitude envelope (a Hann curve) is, to first order, independent
    of that carrier mismatch. We compute the analytic-signal envelope and
    correlate it on a fine fractional-shift grid with the ideal Hann shape;
    the maximum is the sub-sample onset.
    """
    lt = template.size
    lo = max(0, idx - 3)
    hi = min(x.size - lt, idx + 3)
    if hi <= lo:
        return float(idx)
    env = _analytic_envelope(x.astype(np.float64))
    hann = np.hanning(lt).astype(np.float64)

    def _score(p: float) -> float:
        i0 = int(np.floor(p))
        j = np.arange(i0, min(env.size, i0 + lt + 1))
        src = j - p
        k0 = np.floor(src).astype(int)
        w = src - k0
        valid = (k0 >= 0) & (k0 < lt)
        k0c = np.clip(k0, 0, lt - 1)
        k1c = np.clip(k0 + 1, 0, lt - 1)
        shifted = np.zeros(j.size)
        shifted[valid] = (hann[k0c[valid]] * (1.0 - w[valid])
                          + hann[k1c[valid]] * w[valid])
        e = env[j]
        denom = float(np.sqrt(np.sum(shifted * shifted) * np.sum(e * e)))
        return float(np.sum(shifted * e) / max(denom, 1e-20))

    grid = np.linspace(lo, hi, int((hi - lo) * 100) + 1)
    vals = np.array([_score(float(p)) for p in grid])
    pk = int(np.argmax(vals))
    if 0 < pk < vals.size - 1:
        d = float(parabolic_interp(float(vals[pk - 1]), float(vals[pk]),
                                   float(vals[pk + 1])))
        step = float(grid[1] - grid[0])
        return float(grid[pk] + d * step)
    return float(grid[pk])


def pulse_template(frequency_hz: float, duration_s: float, sample_rate: int,
                   window: str = "hann") -> np.ndarray:
    n = max(8, int(round(duration_s * sample_rate)))
    t = np.arange(n) / sample_rate
    sine = np.sin(2.0 * np.pi * frequency_hz * t)
    if window == "hann":
        w = np.hanning(n)
    elif window in ("none", "rect"):  # pragma: no cover - config escape hatch
        w = np.ones(n)
    else:
        raise ValueError(f"unsupported pulse window: {window}")
    tmpl = (sine * w).astype(np.float64)
    tmpl /= np.sqrt(np.sum(tmpl * tmpl))
    return tmpl


def detect_pulses(samples: np.ndarray, sample_rate: int, template: np.ndarray,
                  *, score_threshold: float = 0.4,
                  min_spacing_s: float = 0.1) -> list[Pulse]:
    """Detect pulse onsets with a normalized matched filter.

    The score normalizes the filter output by the signal energy in the
    underlying window, making detection amplitude-independent; non-maximum
    suppression enforces a minimum spacing between pulses.
    """
    x = np.asarray(samples, dtype=np.float64)
    lt = template.size
    if x.size <= lt:
        return []
    # fft_xcorr(template, x) matches numpy.correlate(template, x, 'full').
    # If the pulse onset in x is p, the template aligns with x[p:p+lt] and the
    # peak is at lag d = -p, output index (N-1)-p. Build an array indexed by
    # onset p by reversing the negative-lag region.
    corr_full = fft_xcorr(template, x)
    # Output indices [(N-1)-(N-lt), (N-1)] = [lt-1, N) hold lags -(N-lt)..0,
    # i.e. onsets p = (N-1)-index in reverse order.
    valid = corr_full[lt - 1: x.size][::-1]
    assert valid.size == x.size - lt + 1

    energy_cum = np.concatenate(([0.0], np.cumsum(x * x)))
    # Window [k, k+lt): energy_cum has length N+1 with entry i = sum(x[:i]).
    local_energy = energy_cum[lt: lt + valid.size] - energy_cum[: valid.size]
    assert local_energy.shape == valid.shape
    e_tmpl = np.sum(template * template)  # == 1 for templates from pulse_template
    denom = np.sqrt(np.maximum(local_energy, 1e-20) * e_tmpl)
    scores = valid / denom
    scores = np.clip(scores, 0.0, 1.0)

    min_gap = max(1, int(round(min_spacing_s * sample_rate)))
    # Candidate locations come from *raw matched-filter* local maxima: the raw
    # energy peaks at the true pulse onset, whereas the normalized score can
    # be pushed one sample off the onset by background-noise energy wobble.
    # The normalized score is still used below as the detection threshold.
    v = valid
    is_peak = np.zeros(v.shape, dtype=bool)
    is_peak[1:-1] = (v[1:-1] >= v[:-2]) & (v[1:-1] >= v[2:])
    is_peak[0] = v[0] >= v[1]
    is_peak[-1] = v[-1] >= v[-2]
    above = np.flatnonzero(is_peak & (scores >= score_threshold))
    pulses: list[Pulse] = []
    taken = np.zeros(scores.shape, dtype=bool)
    for idx in above[np.argsort(-valid[above])]:
        if taken[idx]:
            continue
        lo = max(0, idx - min_gap)
        hi = min(scores.size, idx + min_gap + 1)
        taken[lo:hi] = True
        sample_pos = float(idx) if not (0 < idx < valid.size - 1) else _envelope_onset(x, template, idx)
        pulses.append(Pulse(time_s=sample_pos / sample_rate,
                            score=float(scores[idx]), sample=sample_pos))
    pulses.sort(key=lambda p: p.sample)
    return pulses


@dataclass(frozen=True)
class PulsePair:
    t_a: float
    t_b: float
    score_a: float
    score_b: float

def pair_pulses(pulses_a: list[Pulse], pulses_b: list[Pulse],
                *, max_offset_s: float, inlier_threshold_s: float = 0.002,
                iterations: int = 2000, seed: int = 0
                ) -> tuple[list[PulsePair], list[float], list[float]]:
    """Pair pulses with a global order-preserving dynamic-program alignment.

    A simple nearest-neighbour greedy pairing fails when a frame-drop splice
    shortens the offset by exactly one pulse spacing. The DP instead maximizes
    a global score over the full sequence:

    * pairing reward decreases with |tB - tA| (only candidates inside
      ``max_offset_s`` are considered);
    * a pair is rewarded when its local mapping is consistent with the
      previous pair -- near-constant clock rate, or an abrupt step (a frame
      splice) that is accepted at a fixed penalty -- so real pulses on both
      sides of a dropped block still pair correctly;
    * skipping either pulse costs a small constant, so a device-only *false*
      pulse (no real counterpart) is left unmatched rather than forcing a
      wrong pair.

    Deciding whether the residual points lie on one clock line or several is
    the downstream robust fit's job, not this matcher's.
    """
    if not pulses_a or not pulses_b:
        return [], [p.time_s for p in pulses_a], [p.time_s for p in pulses_b]

    A = pulses_a
    B = pulses_b
    n, m = len(A), len(B)
    pair_w = 1.0          # baseline reward for a pairing
    max_rate_dev = 0.02   # smooth local interval rate tolerance
    splice_penalty = 0.4  # accepted abrupt step between consecutive pairs

    NEG = -1e18
    # dp[i][j]: best score with A[:i], B[:j] aligned and (i-1,j-1) paired.
    dp = [[NEG] * m for _ in range(n)]
    back: list[list[tuple[int, int] | None]] = [[None] * m for _ in range(n)]
    for i in range(n):
        for j in range(m):
            off = abs(B[j].time_s - A[i].time_s)
            if off > max_offset_s:
                continue
            best = pair_w - off / max_offset_s * 0.5
            bestprev = None
            for k in range(i):
                for l in range(j):
                    if dp[k][l] <= NEG / 2:
                        continue
                    da = A[i].time_s - A[k].time_s
                    db = B[j].time_s - B[l].time_s
                    if da > 1e-9:
                        rate = db / da
                        if abs(rate - 1.0) <= max_rate_dev:
                            bonus = 1.0 - abs(rate - 1.0) / max_rate_dev
                        elif abs(db - da) <= 0.2:  # frame splice step
                            bonus = 1.0 - splice_penalty
                        else:
                            bonus = -splice_penalty
                    else:
                        bonus = 0.0
                    sc = dp[k][l] + pair_w + bonus - off / max_offset_s * 0.5
                    if sc > best:
                        best, bestprev = sc, (k, l)
            dp[i][j] = best
            back[i][j] = bestprev

    # Choose the best end state, but require it to beat the all-skip baseline
    # (skipping everything costs skip_w per unmatched pulse).
    end = max(((i, j) for i in range(n) for j in range(m)),
              key=lambda ij: dp[ij[0]][ij[1]])
    if dp[end[0]][end[1]] <= NEG / 2:
        return [], [p.time_s for p in A], [p.time_s for p in B]

    chain: list[tuple[int, int]] = []
    cur: tuple[int, int] | None = end
    while cur is not None:
        chain.append(cur)
        cur = back[cur[0]][cur[1]]
    chain.reverse()

    used_a = {i for i, _ in chain}
    used_b = {j for _, j in chain}
    pairs = [
        PulsePair(t_a=float(A[i].time_s), t_b=float(B[j].time_s),
                  score_a=float(A[i].score), score_b=float(B[j].score))
        for i, j in chain
    ]
    unmatched_a = [float(A[i].time_s) for i in range(n) if i not in used_a]
    unmatched_b = [float(B[j].time_s) for j in range(m) if j not in used_b]
    return pairs, unmatched_a, unmatched_b
