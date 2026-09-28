"""Kaiser-windowed-sinc anti-imaging/anti-aliasing prototype FIR.

Design convention (all frequencies in Hz)
-----------------------------------------
For ratio fout/fin = L/M we zero-stuff by L and decimate by M, so the
intermediate ("high") rate is fhigh = L*fin = M*fout.  A single prototype
low-pass at that high rate both removes spectral images (up-sampling) and
prevents aliasing (down-sampling).

* Ideal brick-wall cutoff::

      fc = min(fin, fout) / 2

* Transition band centred on fc, half-width ``half`` (fraction of fc)::

      fpass = (1 - half) * fc        pass-band edge
      fstop = (1 + half) * fc        stop-band edge
      df   = fstop - fpass = 2*half*fc

* Kaiser length rule (the standard Parks-McClellan/Kaiser estimate)::

      numtaps = ceil((A - 7.95) / (2.285 * 2*pi*df/fhigh)) + 1

  rounded up to an **odd** length so the filter is type-I (symmetric around a
  central tap) with an integer group delay H = (numtaps-1)/2 measured in
  high-rate samples.

* beta from the Kaiser formula for stop-band attenuation A (dB).

The normalized design uses the cutoff ``fc/fhigh`` and normalized digital
transition width ``df/fhigh``; sinc taps are evaluated directly in normalized
frequency so no Hz-dependent numerical scaling is involved.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..errors import ResourceExhaustedError
from .ratios import RationalRatio


def bessel_i0(x: float) -> float:
    """Modified Bessel function I0 via power series (self-contained)."""
    total = 1.0
    term = 1.0
    y = (x / 2.0) ** 2
    for k in range(1, 60):
        term *= y / (k * k)
        total += term
        if abs(term) < 1e-20 * total:
            break
    return total


def kaiser_beta(attenuation_db: float) -> float:
    """Kaiser window beta for a target stop-band attenuation (dB)."""
    a = float(attenuation_db)
    if a > 50.0:
        return 0.1102 * (a - 8.7)
    if a >= 21.0:
        return 0.5842 * (a - 21.0) ** 0.78 + 0.07886 * (a - 21.0)
    return 0.0


def kaiser_numtaps(attenuation_db: float, digital_width: float) -> int:
    """Odd filter length for attenuation and normalized transition width.

    ``digital_width`` is (fstop - fpass)/fhigh, a fraction of the high-rate
    sample rate (so the digital radian width is 2π·digital_width).  Standard
    Kaiser length rule::

        N = ceil((A - 7.95) / (2.285 · 2π · digital_width)) + 1

    e.g. A=80 dB, width=0.025 -> 201 taps.
    """
    n = math.ceil((attenuation_db - 7.95)
                  / (2.285 * 2.0 * math.pi * digital_width)) + 1
    if n % 2 == 0:
        n += 1  # round up to an odd (type-I) symmetric length
    return max(n, 3)


@dataclass(frozen=True)
class FilterDesign:
    ratio: RationalRatio
    numtaps: int
    half: int                       # H: group delay in high-rate samples
    beta: float
    cutoff_hz: float                # ideal brick-wall fc
    fpass_hz: float
    fstop_hz: float
    transition_hz: float
    attenuation_db: float
    coeffs: np.ndarray              # float64 prototype, DC gain == L

    @property
    def group_delay_high_samples(self) -> int:
        return self.half

    @property
    def group_delay_input_samples(self) -> float:
        return self.half / self.ratio.l

    @property
    def group_delay_output_samples(self) -> float:
        return self.half / self.ratio.m

    def design_summary(self) -> dict:
        return {
            "numtaps": self.numtaps,
            "half_taps": self.half,
            "beta": self.beta,
            "cutoff_hz": self.cutoff_hz,
            "fpass_hz": self.fpass_hz,
            "fstop_hz": self.fstop_hz,
            "transition_hz": self.transition_hz,
            "attenuation_db": self.attenuation_db,
            "group_delay_high_samples": self.group_delay_high_samples,
            "group_delay_input_samples": self.group_delay_input_samples,
            "group_delay_output_samples": self.group_delay_output_samples,
            "group_delay_seconds": self.half / self.ratio.high_rate,
        }


def design_prototype(ratio: RationalRatio, *, attenuation_db: float = 80.0,
                     transition_half_width: float = 0.1,
                     max_taps: int = 2_000_001) -> FilterDesign:
    """Design the high-rate prototype low-pass for ``ratio``."""
    if not (0.0 < transition_half_width < 1.0):
        raise ValueError("transition_half_width must lie in (0, 1)")
    if attenuation_db < 21.0:
        raise ValueError("attenuation_db must be >= 21")

    fhigh = ratio.high_rate
    fc = min(ratio.fin, ratio.fout) / 2.0
    fpass = (1.0 - transition_half_width) * fc
    fstop = (1.0 + transition_half_width) * fc
    digital_width = (fstop - fpass) / fhigh  # fraction of fhigh
    beta = kaiser_beta(attenuation_db)
    numtaps = kaiser_numtaps(attenuation_db, digital_width)

    if numtaps > max_taps:
        raise ResourceExhaustedError(
            f"prototype FIR length {numtaps} exceeds limit {max_taps}; "
            "widen the transition band or choose a less extreme ratio",
            details={"numtaps": numtaps, "max_taps": max_taps,
                     "digital_width": digital_width},
        )
    half = numtaps // 2
    # The shortest arm needs one interior tap (d == 0 at k=0).  When the
    # prototype is shorter than L taps this cannot be guaranteed.
    if half < ratio.l - 1:
        raise ResourceExhaustedError(
            f"prototype half-length H={half} smaller than L-1={ratio.l - 1}; "
            "lengthen the filter (narrower transition band / more attenuation)",
            details={"half": half, "l": ratio.l},
        )

    n = np.arange(-half, half + 1, dtype=np.float64)
    wc = 2.0 * math.pi * (fc / fhigh)
    with np.errstate(divide="ignore", invalid="ignore"):
        ideal = np.where(n == 0, wc / math.pi, np.sin(wc * n) / (math.pi * n))
    arg = 1.0 - (n / half) ** 2
    window = np.i0(beta * np.sqrt(np.maximum(arg, 0.0))) / bessel_i0(beta) \
        if beta > 0.0 else np.ones_like(n)
    coeffs = ideal * window
    # Unit DC gain on the high-rate (zero-stuffed) stream; after interpolation
    # scaling by L the overall DC gain input->output is exactly 1.
    coeffs *= ratio.l / float(np.sum(coeffs))
    return FilterDesign(
        ratio=ratio, numtaps=numtaps, half=half, beta=beta,
        cutoff_hz=fc, fpass_hz=fpass, fstop_hz=fstop,
        transition_hz=fstop - fpass, attenuation_db=attenuation_db,
        coeffs=np.asarray(coeffs, dtype=np.float64),
    )
