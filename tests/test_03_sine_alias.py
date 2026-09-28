"""Sinusoidal fidelity, aliasing suppression (down) and imaging suppression (up).

All test tones are generated with an integer number of cycles over a block,
so amplitudes are measured by coherent DFT correlation -- no window, no
leakage, no window-gain bookkeeping.  Suppression numbers are amplitude dB
relative to the input tone amplitude.
"""

from __future__ import annotations

import numpy as np

from resampler.signal import StreamingPolyphase, build_plan


def coherent_cycles(sample_rate: float, freq: float, cycles: int) -> int:
    """Smallest sample count >= floor so the tone has exactly `cycles` cycles."""
    return int(round(cycles * sample_rate / freq))


def tone(sample_rate, freq_hz, n, amp=0.7, phase=0.0):
    t = np.arange(n) / sample_rate
    return amp * np.sin(2 * np.pi * freq_hz * t + phase)


def coherent_amp(x: np.ndarray, sample_rate: float, freq_hz: float) -> float:
    """Amplitude at exactly freq_hz, assuming integer cycles over len(x)."""
    n = x.size
    phasor = np.exp(-2j * np.pi * freq_hz * np.arange(n) / sample_rate)
    return 2.0 * abs(np.dot(x.astype(np.float64), phasor)) / n


def stream_all(plan, x, chunk=256):
    eng = StreamingPolyphase(plan)
    outs = []
    for i in range(0, x.size, chunk):
        outs.append(eng.push(x[i:i + chunk]))
    outs.append(eng.flush())
    return np.concatenate(outs)


def trim_coherent(y, f_out, plan, probe_freqs):
    """Trim both edges by the group delay, then retain a length over which
    every probe tone completes an integer number of cycles.

    A tone at f0 has integer cycles at length n when n*f0/f_out is integer.
    The common period of the probe set is f_out/gcd(probe frequencies).
    """
    import math
    edge = int(np.ceil(plan.delay_output)) + plan.taps_per_phase
    z = y[edge:y.size - edge]
    # Smallest p with p*f_i/f_out integer for every probe:
    # p_i = f_out/gcd(f_out, f_i); common p = lcm(p_i).
    period = 1
    for f0 in probe_freqs:
        fi = int(round(f0))
        period = period * (int(f_out) // math.gcd(int(f_out), fi)) // \
            math.gcd(period, int(f_out) // math.gcd(int(f_out), fi))
    m = (z.size // period) * period
    return z[:m]


def test_low_freq_sine_passthrough_down(runlog):
    f_in, f_out, f0 = 48000, 16000, 1000.0
    n = coherent_cycles(f_in, f0, 128)          # integer cycles
    x = tone(f_in, f0, n)
    plan = build_plan(f_in, f_out)
    y = trim_coherent(stream_all(plan, x, chunk=512), f_out, plan, [1000.0])

    gain = 20 * np.log10(coherent_amp(y, f_out, f0) / 0.7)
    runlog.check("1 kHz tone gain within +/-0.05 dB (3x down)",
                 abs(gain) <= 0.05, {"gain_db": gain},
                 "1 kHz is well inside 7.2 kHz passband edge")


def test_above_new_nyquist_is_rejected_down(runlog):
    """12 kHz at f_in=48k would alias to 4 kHz under /3 without filtering."""
    f_in, f_out, f_bad = 48000, 16000, 12000.0
    n = coherent_cycles(f_in, f_bad, 4096)
    x = tone(f_in, f_bad, n, amp=0.8)
    plan = build_plan(f_in, f_out)
    y = trim_coherent(stream_all(plan, x, chunk=1000), f_out, plan, [4000.0])

    alias_amp = coherent_amp(y, f_out, 4000.0)
    supp = 20 * np.log10((alias_amp + 1e-12) / 0.8)
    runlog.check("12 kHz alias image at 4 kHz suppressed >= 60 dB",
                 supp <= -60.0, {"alias_gain_db": supp},
                 "anti-alias stopband begins at 8 kHz")

    # Total residual energy: a 12 kHz tone has no baseband content at all,
    # so the whole interior must be tiny relative to the input.
    rms_db = 20 * np.log10(np.sqrt(np.mean(y ** 2)) / 0.8 + 1e-12)
    runlog.check("interior RMS residual of rejected tone <= -55 dB",
                 rms_db <= -55.0, {"rms_db": float(rms_db)},
                 "end-to-end suppression after edge trimming")


def test_passband_edge_tone_survives(runlog):
    f_in, f_out, f0 = 48000, 16000, 7000.0
    n = coherent_cycles(f_in, f0, 896)
    x = tone(f_in, f0, n)
    plan = build_plan(f_in, f_out)
    y = trim_coherent(stream_all(plan, x, chunk=333), f_out, plan, [7000.0])
    gain = 20 * np.log10(coherent_amp(y, f_out, f0) / 0.7)
    runlog.check("7 kHz in-band tone gain within +/-0.2 dB",
                 abs(gain) <= 0.2, {"gain_db": float(gain)},
                 "inside 0.9*Nyquist passband edge")


def test_upsample_images_suppressed(runlog):
    """3x up: zero-stuff images around F_in +- f0, 2F_in +- f0 must be gone."""
    f_in, f_out, f0 = 16000, 48000, 2000.0
    n = coherent_cycles(f_in, f0, 128)
    x = tone(f_in, f0, n)
    plan = build_plan(f_in, f_out)
    y = trim_coherent(stream_all(plan, x, chunk=128), f_out, plan, [2000.0, 14000.0, 18000.0, 30000.0, 34000.0])

    gain = 20 * np.log10(coherent_amp(y, f_out, f0) / 0.7)
    runlog.check("2 kHz tone gain within +/-0.05 dB (3x up)",
                 abs(gain) <= 0.05, {"gain_db": float(gain)}, "passband flatness")

    for f_image in (14000.0, 18000.0, 30000.0, 34000.0):
        a = coherent_amp(y, f_out, f_image)
        supp = 20 * np.log10((a + 1e-12) / 0.7)
        runlog.check(f"image at {f_image/1000:.0f} kHz suppressed >= 60 dB",
                     supp <= -60.0, {"image_gain_db": float(supp)},
                     "zero-stuff spectral images removed by anti-image LPF")


def test_two_tone_pack(runlog):
    """One in-band tone (3 kHz) + one above common Nyquist (23 kHz), 44.1->48k."""
    f_in, f_out = 44100, 48000
    n = coherent_cycles(f_in, 1000.0, 200) * 20      # multiple coherent for both
    tt = np.arange(n) / f_in
    x = 0.5 * np.sin(2 * np.pi * 3000 * tt) + 0.5 * np.sin(2 * np.pi * 23000 * tt)
    plan = build_plan(f_in, f_out)
    y = trim_coherent(stream_all(plan, x, chunk=1024), f_out, plan, [3000.0, 1000.0])
    g_good = 20 * np.log10(coherent_amp(y, f_out, 3000.0) / 0.5)
    runlog.check("44.1->48k: 3 kHz tone preserved", abs(g_good) <= 0.2,
                 {"gain_db": float(g_good)}, "inside common passband")
    # 23 kHz is above min-Nyquist 22.05 kHz; its nearest up-sampled image is
    # 48-23=25 kHz (beyond 24k output Nyquist, thus unobservable), and baseband
    # leak concentrates at the 22.05-24k transition.  Scan interior RMS there
    # via a coherent probe at 23 kHz alias: |23-?| nearest output representation.
    a = coherent_amp(y, f_out, 1000.0)   # guard band: no source at 1 kHz
    runlog.check("no spurious coherent content at 1 kHz",
                 20 * np.log10((a + 1e-12) / 0.5) <= -60.0,
                 {"probe_db": float(20 * np.log10((a + 1e-12) / 0.5))},
                 "both inputs are far from 1 kHz")
