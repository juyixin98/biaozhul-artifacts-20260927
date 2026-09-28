"""Signal-domain tests against the independent FFT reference.

Coverage required by the review brief:
* low-frequency sine — amplitude/frequency fidelity vs. reference and theory;
* tone above the *new* Nyquist — aliasing suppressed by a concrete margin;
* a naive decimation control that MUST show aliasing (proves the test bites);
* up-sampling imaging suppression;
* agreement of every interior sample with the independent off-line reference
  (reference is zero-stuff + FFT convolution, not the polyphase code).
"""
from __future__ import annotations

import numpy as np
import pytest

from resamp.dsp.fir import design_prototype
from resamp.dsp.polyphase import PolyphaseResampler
from resamp.dsp.reference import resample_offline
from resamp.dsp.ratios import RationalRatio


def _tone(fin, f0, n, amp=0.7, phase=0.3):
    t = np.arange(n) / fin
    return amp * np.sin(2 * np.pi * f0 * t + phase)


def _run(fin, fout, x, chunk=4096, **kw):
    rs = PolyphaseResampler(fin, fout, **kw)
    parts = []
    for i in range(0, x.size, chunk):
        parts.append(rs.push(x[i:i + chunk]))
    parts.append(rs.flush())
    return np.concatenate(parts), rs.design


def _parabolic_peak(mag: np.ndarray, k: int):
    """Quadratic interpolation of a spectral peak around bin k."""
    a, b, c = mag[k - 1], mag[k], mag[k + 1]
    denom = (a - 2 * b + c)
    delta = 0.5 * (a - c) / denom if denom != 0 else 0.0
    peak = b - 0.25 * (a - c) * delta
    return peak, delta


def _steady_tone_metrics(y, fout, f0, trim):
    z = y[trim:-trim]
    n = z.size
    win = np.hanning(n)
    spec = np.fft.rfft(z * win)
    mag = np.abs(spec)
    freqs = np.fft.rfftfreq(n, d=1.0 / fout)
    k = int(np.argmin(np.abs(freqs - f0)))
    _, delta = _parabolic_peak(mag, k)
    power = mag ** 2
    # Signal energy is the fitted main-lobe neighborhood; the noise floor is
    # measured only away from the lobe, so Hann side-lobe leakage of the
    # (correctly preserved) tone is not counted as distortion.
    lobe = (np.abs(freqs - f0) <= 3.0 * fout / n)
    signal_e = power[lobe].sum()
    away = ~lobe
    noise_e = power[away].sum() / max(away.sum(), 1) * lobe.sum()
    # Amplitude is measured with a flat-top window, whose pass band is flat to
    # a small fraction of a dB even between FFT bins (Hann scallops ~1.5 dB).
    # Coefficients are the standard Matplotlib/HP 4-term periodic flattop with
    # coherent gain 0.21557895.
    c0, c1, c2, c3 = (0.21557895, 0.41663158, 0.277263158, 0.083578947)
    ii = np.arange(n)
    flat = (c0 - c1 * np.cos(2 * np.pi * ii / n)
            + c2 * np.cos(4 * np.pi * ii / n)
            - c3 * np.cos(6 * np.pi * ii / n))
    fmag = np.abs(np.fft.rfft(z * flat))
    fk = int(np.argmin(np.abs(np.fft.rfftfreq(n, 1.0 / fout) - f0)))
    amp = 2.0 * fmag[fk] / float(flat.sum())
    snr_db = 10 * np.log10(signal_e / max(noise_e, 1e-300))
    f_meas = freqs[k] + delta * (fout / n)
    return amp, snr_db, f_meas


@pytest.mark.parametrize("fin,fout,f0", [
    (8000, 16000, 440),
    (8000, 48000, 1000),
    (44100, 48000, 2500),
    (48000, 44100, 1234),
    (16000, 8000, 300),
])
def test_low_frequency_tone_preserved(fin, fout, f0, log):
    n = 40000
    x = _tone(fin, f0, n, amp=0.7)
    y, design = _run(fin, fout, x)
    yr, _ = resample_offline(x, fin, fout)
    err = np.abs(y - yr)
    log("interior agreement", ok=bool(err.max() < 1e-9),
        reason="polyphase vs independent FFT reference",
        max_abs_err=float(err.max()),
        rms_err=float(np.sqrt(np.mean(err ** 2))))
    assert err.max() < 1e-9

    trim = int(2 * design.group_delay_output_samples) + 200
    amp, snr_db, f_meas = _steady_tone_metrics(y, fout, f0, trim)
    log("tone fidelity", ok=bool(abs(amp - 0.7) < 2e-3 and snr_db > 70
                                  and abs(f_meas - f0) <= fout / (y.size - 2 * trim)),
        reason="amplitude within 0.2%, SNR > 70 dB, bin matches f0",
        measured_amp=amp, expected_amp=0.7, snr_db=snr_db,
        measured_freq=f_meas, expected_freq=f0)
    assert abs(amp - 0.7) < 2e-3
    assert snr_db > 70.0
    assert abs(f_meas - f0) <= fout / (y.size - 2 * trim) + 1e-9


@pytest.mark.parametrize("fin,fout,f_tone", [
    (16000, 8000, 6500),    # > new Nyquist 4000; alias would land at 1500 Hz
    (48000, 16000, 12500),  # > 8000; naive alias folds to 4500 Hz
])
def test_above_new_nyquist_is_removed(fin, fout, f_tone, log):
    n = 60000
    x = _tone(fin, f_tone, n, amp=0.9)
    y, design = _run(fin, fout, x)
    trim = int(2 * design.group_delay_output_samples) + 500
    z = y[trim:-trim]
    nn = z.size
    win = np.hanning(nn)
    spec = np.abs(np.fft.rfft(z * win))
    freqs = np.fft.rfftfreq(nn, d=1.0 / fout)
    # Folding of f_tone through the integer M=fin/gcd rate reduction.
    r = RationalRatio.reduce(fin, fout)
    folded = f_tone % fout
    expected_alias = min(folded, fout - folded)
    alias_bin = int(np.argmin(np.abs(freqs - expected_alias)))
    # Hann amplitude normalization factor 2/sum(w).
    alias_db = 20 * np.log10(2.0 * spec[alias_bin] / win.sum() + 1e-300)
    inband_peak = 2.0 * spec.max() / win.sum()
    inband_db = 20 * np.log10(inband_peak + 1e-300)
    log("alias suppression",
        ok=bool(alias_db < -55.0 and inband_db < -50.0),
        reason="content above new Nyquist suppressed by >=55 dB; "
               "no in-band tone above -50 dB",
        alias_freq_hz=expected_alias, alias_level_db=alias_db,
        inband_peak_db=inband_db, fstop_hz=design.fstop_hz)
    assert alias_db < -55.0
    assert inband_db < -50.0


def test_naive_decimation_control_does_alias(log):
    """Control experiment: dropping samples with no filter must alias loudly.
    If this ever fails, the alias test above could pass for the wrong reason."""
    fin, fout, f_tone = 16000, 8000, 6500
    n = 60000
    x = _tone(fin, f_tone, n, amp=0.9)
    y = x[::2]
    z = y[1000:-1000]
    nn = z.size
    win = np.hanning(nn)
    spec = np.abs(np.fft.rfft(z * win))
    freqs = np.fft.rfftfreq(nn, d=1.0 / fout)
    alias_at = 1500.0
    k = int(np.argmin(np.abs(freqs - alias_at)))
    level = 20 * np.log10(2.0 * spec[k] / win.sum())
    log("naive control", ok=bool(level > -12.0),
        reason="unfiltered 2:1 decimation leaves a strong ~1500 Hz alias",
        alias_level_db=level)
    assert level > -12.0


def test_upsampling_images_removed(log):
    fin, fout, f0 = 8000, 48000, 2000
    x = _tone(fin, f0, 40000, amp=0.8)
    y, design = _run(fin, fout, x)
    trim = int(2 * design.group_delay_output_samples) + 500
    z = y[trim:-trim]
    nn = z.size
    win = np.hanning(nn)
    spec = 2.0 * np.abs(np.fft.rfft(z * win)) / win.sum()
    freqs = np.fft.rfftfreq(nn, d=1.0 / fout)
    # Zero-stuff images of a 2 kHz tone sit at L*fin +/- f0: 14k, 22k, 26k, ...
    image_freqs = [2 * fin - f0, 2 * fin + f0,
                   4 * fin - f0, 4 * fin + f0,
                   6 * fin - f0]
    worst = -200.0
    for fc in image_freqs:
        if fc < fout / 2:
            k = int(np.argmin(np.abs(freqs - fc)))
            worst = max(worst, 20 * np.log10(spec[k] + 1e-300))
    log("images", ok=bool(worst < -55.0),
        reason="zero-stuff spectral images suppressed >=55 dB",
        image_freqs_hz=[f for f in image_freqs if f < fout / 2],
        worst_image_db=worst)
    assert worst < -55.0


@pytest.mark.parametrize("fin,fout", [(8000, 16000), (44100, 48000),
                                      (48000, 44100), (16000, 8000)])
def test_interior_samples_match_reference_to_1e_9(fin, fout, log):
    rng = np.random.default_rng(11)
    # Multi-band content well inside the pass band.
    n = 30000
    x = (0.4 * np.sin(2 * np.pi * 200 * np.arange(n) / fin)
         + 0.3 * np.sin(2 * np.pi * 900 * np.arange(n) / fin)
         + 0.1 * rng.standard_normal(n))
    y, design = _run(fin, fout, x, chunk=37)
    yr, _ = resample_offline(x, fin, fout)
    assert y.shape == yr.shape
    err = np.abs(y - yr)
    log("reference agreement", ok=bool(err.max() < 1e-9),
        reason="streaming polyphase equals FFT reference to 1e-9",
        max_abs_err=float(err.max()),
        n_out=int(y.size))
    assert err.max() < 1e-9
