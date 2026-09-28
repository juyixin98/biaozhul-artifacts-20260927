"""Kaiser-windowed sinc low-pass and the polyphase resampling plan.

Fixed design contract (see docs/DESIGN.md for the derivation):

* Ratio is L/M (coprime), high-rate grid at F_h = L*f_in.
* Anti-aliasing / anti-imaging stopband edge (on F_h):
      f_sb / F_h = min(1/L, 1/M)
  i.e. min(f_in, f_out)/2 expressed on the high-rate grid.
* Passband edge = passband_edge * stopband edge (default 0.9).
* Prototype: windowed sinc with cutoff at the midpoint of the transition
  band, symmetric (zero-phase) Kaiser window of length N = K*L.
* K (taps per polyphase column) is even; prototype group delay on the
  high-rate grid is (N-1)/2, i.e. (K-1)/2 input samples.
* The *whole prototype* is scaled so sum(h) == L exactly: the zero-stuff
  upsample path then has DC gain 1 and the combined resampler DC gain is
  1.  Per-column sums vary by the intrinsic sinc-truncation ripple only
  (sub-1e-5 at these lengths); normalizing each column independently
  would redistribute the designed gain across phases and distort the
  Kaiser transition shape, so it is deliberately NOT done.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..errors import ComputationError, InputValidationError, ResourceExhaustedError
from .ratio import RationalRatio, reduce_ratio


def kaiser_beta(atten_db: float) -> float:
    """Kaiser shape parameter beta from stopband attenuation (dB)."""
    if atten_db > 50.0:
        return 0.1102 * (atten_db - 8.7)
    if atten_db >= 21.0:
        return 0.5842 * (atten_db - 21.0) ** 0.4 + 0.07886 * (atten_db - 21.0)
    return 0.0


def _kaiser_window(n: int, beta: float) -> np.ndarray:
    """Symmetric Kaiser window of length n (I0 series, no SciPy)."""
    if n == 1:
        return np.ones(1, dtype=np.float64)
    idx = np.arange(n, dtype=np.float64)
    x = 2.0 * idx / (n - 1) - 1.0                    # -1 .. 1
    arg = beta * np.sqrt(np.maximum(0.0, 1.0 - x * x))
    # I0(z) series: sum ( (z/2)^k / k! )^2
    z = arg / 2.0
    term = np.ones_like(z)
    total = np.ones_like(z)
    for k in range(1, 40):
        term = term * (z / k)
        total = total + term * term
    return total / total[n // 2]                    # I0(beta*sqrt(1-x^2))/I0(beta)


@dataclass(frozen=True)
class ResamplePlan:
    ratio: RationalRatio
    taps_per_phase: int                  # K
    atten_db: float
    passband_edge_frac: float            # pb (fraction of stopband edge)
    prototype: np.ndarray                # h, length N = K*L, float64
    polyphase: np.ndarray                # P, shape (L, K), float64; sum(P)==L over all
    delay_high: float                    # (N-1)/2, high-rate samples
    delay_input: float                   # (K-1)/2, input samples
    delay_output: float                  # delay_input * L/M, output samples
    passband_edge_hz: float              # passband edge in Hz at the *lower* Nyquist
    stopband_edge_hz: float              # stopband edge in Hz (= min(f_in,f_out)/2)
    cutoff_hz: float                     # -6 dB-ish midpoint cutoff, Hz on low side
    output_count_total: int              # populated only for known J: -1 if unknown

    @property
    def up(self) -> int:
        return self.ratio.up

    @property
    def down(self) -> int:
        return self.ratio.down

    @property
    def num_taps(self) -> int:
        return self.taps_per_phase * self.up

    def expected_outputs(self, input_samples: int) -> int:
        """Number of output samples for a finite stream of J input samples.

        Leading zero transient is included (n starts at 0).  For J >= 1:
            n_max = ceil(L*(J+K-1)/M) - 1
        An empty stream produces zero outputs.
        """
        J = int(input_samples)
        if J <= 0:
            return 0
        L, M, K = self.up, self.down, self.taps_per_phase
        return math.ceil(L * (J + K - 1) / M)

    def describe(self) -> dict:
        return {
            "up": self.up,
            "down": self.down,
            "taps_per_phase": self.taps_per_phase,
            "num_taps": self.num_taps,
            "attenuation_db": self.atten_db,
            "passband_edge_fraction": self.passband_edge_frac,
            "passband_edge_hz": self.passband_edge_hz,
            "stopband_edge_hz": self.stopband_edge_hz,
            "cutoff_hz": self.cutoff_hz,
            "group_delay": {
                "high_rate_samples": self.delay_high,
                "input_samples": self.delay_input,
                "output_samples": self.delay_output,
            },
        }


def build_plan(input_rate: float | int, output_rate: float | int,
               atten_db: float | None = None,
               passband_edge: float | None = None,
               settings=None) -> ResamplePlan:
    """Construct the validated resampling plan."""
    from ..config import Settings
    s = settings or Settings()

    atten = float(s.default_atten_db if atten_db is None else atten_db)
    pb = float(s.default_passband_edge if passband_edge is None else passband_edge)
    if not math.isfinite(atten) or atten <= 0:
        raise InputValidationError("attenuation_db must be a positive finite number",
                                   {"got": atten_db})
    if not (0.0 < pb < 1.0):
        raise InputValidationError("passband_edge must lie in (0, 1)",
                                   {"got": passband_edge})

    ratio: RationalRatio = reduce_ratio(input_rate, output_rate, s.max_ratio_term)
    L, M = ratio.up, ratio.down

    # Normalized to F_h = L*f_in (1.0 == F_h; Nyquist == 0.5):
    # new/common Nyquist at min(f_in,f_out)/2 = 0.5*min(1/L,1/M)*F_h.
    sb_norm = 0.5 * min(1.0 / L, 1.0 / M)  # stopband edge, cycles/high sample
    pb_norm = pb * sb_norm                 # passband edge
    delta = sb_norm - pb_norm              # transition width, cycles / high sample

    beta = kaiser_beta(atten)
    # Kaiser order estimate (number of taps for the prototype):
    n_est = (atten - 7.95) / (14.36 * delta)
    K = int(math.ceil(n_est / L))
    K = max(K, int(s.min_taps_per_phase))
    if K % 2 == 1:
        K += 1                                 # even K -> even prototype, exact half-grid center
    N = K * L
    if N > s.max_filter_taps:
        raise ResourceExhaustedError(
            "prototype filter exceeds configured tap cap",
            {"num_taps": N, "cap": s.max_filter_taps,
             "up": L, "down": M, "taps_per_phase": K})

    fc_norm = 0.5 * (pb_norm + sb_norm)        # cutoff at transition midpoint, cycles/sample
    # np.sinc(u) = sin(pi u)/(pi u).  The ideal discrete-time low-pass impulse
    # response is 2*fc*sinc(2*fc*t); we build the *unnormalized* windowed sinc
    # and apply a single L/sum scale so that sum(h)==L (DC gain exactly 1),
    # which also restores the intrinsic 2*fc height in one step.
    t = np.arange(N, dtype=np.float64) - (N - 1) / 2.0
    h0 = np.sinc(2.0 * fc_norm * t) * _kaiser_window(N, beta)

    # Polyphase: P[p, a] = h[a*L + p], shape (L, K).
    # Global normalization: sum(h) == L makes the resampler DC gain exactly 1
    # while preserving the designed Kaiser transition shape.
    total = float(h0.sum())
    if not np.isfinite(total) or total == 0.0:
        raise ComputationError("degenerate prototype (zero/non-finite sum)",
                               {"up": L, "down": M})
    prototype = h0 * (L / total)
    poly = prototype.reshape(K, L).T.copy()

    f_nyq_low = min(ratio.rate_in, ratio.rate_out) / 2.0
    plan = ResamplePlan(
        ratio=ratio,
        taps_per_phase=K,
        atten_db=atten,
        passband_edge_frac=pb,
        prototype=prototype,
        polyphase=np.ascontiguousarray(poly, dtype=np.float64),
        delay_high=(N - 1) / 2.0,
        delay_input=(K - 1) / 2.0,
        delay_output=(K - 1) / 2.0 * L / M,
        passband_edge_hz=pb * f_nyq_low,
        stopband_edge_hz=f_nyq_low,
        cutoff_hz=0.5 * (pb + 1.0) * f_nyq_low,
        output_count_total=-1,
    )
    return plan
