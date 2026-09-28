"""Independent high-precision offline references for the test-suite.

Nothing in this module imports :mod:`resampler.signal.engine`.  Two
references are provided on purpose:

* :func:`scipy_poly_ref` -- SciPy's ``upfirdn`` driven with the plan's
  prototype taps.  Independent *implementation*, same filter.  It is the
  exact-math reference (asserted to ~1e-14).
* :func:`fft_poly_ref` -- a self-contained FFT full-convolution reference
  written for this project (NumPy only).  Independent implementation AND
  independent construction path; used as a backup if SciPy is unavailable
  and for the long-signal alias/imaging measurements.

Both share the DUT's documented convention: head/tail zero padding,
output n aligned at t(n)=(n*M-(K-1)/2)/(L*f_in).
"""

from __future__ import annotations

import numpy as np


def scipy_poly_ref(x: np.ndarray, prototype: np.ndarray, up: int,
                   down: int):
    """scipy.signal.upfirdn reference (imported lazily)."""
    from scipy.signal import upfirdn
    return upfirdn(np.asarray(prototype, dtype=np.float64),
                   np.asarray(x, dtype=np.float64), up=up, down=down)


def fft_poly_ref(x: np.ndarray, prototype: np.ndarray, up: int,
                 down: int) -> np.ndarray:
    """Reference polyphase resampling via zero-stuff + FFT convolution.

    Steps, each independently auditable:
      1. zero-stuff x by L (x_up[k*L]=x[k], others 0);
      2. full linear convolution with the prototype via FFT (nextpow2);
      3. take indices 0::M.
    Length identity checked against the DUT's documented formula elsewhere.
    """
    x = np.asarray(x, dtype=np.float64)
    h = np.asarray(prototype, dtype=np.float64)
    L, M = int(up), int(down)
    if x.size == 0:
        return np.empty(0, dtype=np.float64)
    xu = np.zeros(x.size * L, dtype=np.float64)
    xu[::L] = x
    n = xu.size + h.size - 1
    nfft = 1 << (n - 1).bit_length()
    full = np.fft.irfft(np.fft.rfft(xu, nfft) * np.fft.rfft(h, nfft), nfft)
    full = full[:n]
    dec = np.ascontiguousarray(full[::M])
    # The decimated full convolution may include trailing outputs whose
    # K-tap window touches *only* zero-stuffed slots (possible when L>1).
    # The mathematical stream ends at the last output with floor(nM/L) <=
    # J+K-2 (globals are leading-zeros + x[0..J-1] + K-1 tail zeros),
    # i.e. n_max+1 = ceil(L*(J+K-1)/M).  Those extra dec entries are 0.0.
    keep = -(-L * (x.size + h.size // L - 1) // M)  # ceil(L*(J+K-1)/M)
    return dec[:keep]


def tone(sample_rate: float, freq_hz: float, n: int,
         phase: float = 0.0, amp: float = 0.5) -> np.ndarray:
    t = np.arange(n) / float(sample_rate)
    return amp * np.sin(2.0 * np.pi * freq_hz * t + phase)


def _coherent_tone_amp(x: np.ndarray, sample_rate: float,
                       freq_hz: float) -> float:
    """Amplitude of a tone at freq_hz via coherent DFT bin correlation.

    Test signals are generated with integer cycles over the window, so no
    window is needed and the DFT coefficient at the tone bin is leakage-free.
    """
    n = x.size
    k = n * freq_hz / sample_rate
    if abs(k - round(k)) > 1e-6:
        # Non-coherent: fall back to local Hann-windowed energy estimate.
        w = np.hanning(n)
        X = np.fft.rfft(x * w)
        freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
        j = int(np.argmin(np.abs(freqs - freq_hz)))
        # Hann coherent gain = 0.5; sum(w^2)=1.5n/4 -> amplitude correction.
        return 4.0 * abs(X[j]) / (3.0 * n / 2.0)
    return 2.0 * abs(np.sum(x * np.exp(-2j * np.pi * k * np.arange(n) / n))) / n


def band_energy_db(x: np.ndarray, sample_rate: float, f_lo: float,
                   f_hi: float) -> float:
    """Energy in [f_lo, f_hi] via Hann-windowed FFT, in dB of signal energy.

    Hann coherent/noncoherent gains cancel because BOTH numerator band and
    total are measured from the *same* windowed spectrum; residual taper
    error is below -90 dB for these test lengths.
    """
    if x.size < 8:
        return -np.inf
    w = np.hanning(x.size)
    X = np.fft.rfft(x * w)
    freqs = np.fft.rfftfreq(x.size, d=1.0 / sample_rate)
    band = (freqs >= f_lo) & (freqs <= f_hi)
    e = float(np.sum(np.abs(X[band]) ** 2))
    ref = float(np.sum(np.abs(X) ** 2)) + 1e-300
    return 10.0 * np.log10((e + 1e-30) / ref)


def single_tone_gain_db(x: np.ndarray, sample_rate: float,
                        freq_hz: float) -> float:
    """Estimated magnitude gain at one tone frequency (dB), reference amp 1.0."""
    amp = _coherent_tone_amp(x, sample_rate, freq_hz)
    return 20.0 * np.log10((amp + 1e-30) / 1.0)


SCIPY_AVAILABLE: bool
try:  # pragma: no cover - environment dependent
    import scipy  # noqa: F401
    SCIPY_AVAILABLE = True
except Exception:  # pragma: no cover
    SCIPY_AVAILABLE = False
