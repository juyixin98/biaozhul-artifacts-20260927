"""Independent reference implementations.

Nothing here imports the project kernel. These oracles are how we prove the
kernel is correct rather than self-consistent:

  * :class:`PyloudnormReference` wraps the third-party ``pyloudnorm`` meter
    (independent filtering, blocking and gating code path). It provides
    integrated loudness only. pyloudnorm 0.2.0 uses a 97%-overlap,
    linearly-interpolated LRA implementation that intentionally diverges from
    EBU Tech 3342 / ffmpeg, so it is NOT used as an LRA oracle.

  * :class:`FfmpegReference` shells out to the locally installed ``ffmpeg``
    ebur128 filter (an independent C implementation) and parses both integrated
    loudness, its gate, and the LRA summary. This is the authoritative oracle
    for every gated statistic. If ffmpeg is unavailable the reference reports
    ``available=False`` and tests that need it skip, recording that fact.
"""
from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .fixtures import to_wav


@dataclass
class FfmpegSummary:
    available: bool
    integrated_lufs: float | None
    integrated_threshold_lufs: float | None
    lra_lu: float | None
    lra_threshold_lufs: float | None
    lra_low_lufs: float | None
    lra_high_lufs: float | None
    raw: str = ""

    @property
    def is_silence_sentinel(self) -> bool:
        # ffmpeg prints I=-70.0 and LRA thresholds of 0.0 on pure silence;
        # that is a sentinel, not a measured gated result.
        return (self.integrated_lufs == -70.0
                and self.integrated_threshold_lufs == 0.0)


class FfmpegReference:
    def __init__(self, binary: str = "ffmpeg"):
        self.binary = shutil.which(binary) or ""
        self.available = bool(self.binary)
        self.version = ""
        if self.available:
            out = subprocess.run([self.binary, "-version"],
                                 capture_output=True, text=True)
            self.version = out.stdout.splitlines()[0] if out.stdout else ""

    def measure(self, samples: np.ndarray) -> FfmpegSummary:
        if not self.available:
            return FfmpegSummary(False, None, None, None, None, None, None)
        with tempfile.TemporaryDirectory() as td:
            wav_path = Path(td) / "ref.wav"
            wav_path.write_bytes(to_wav(samples, "s16"))
            proc = subprocess.run(
                [self.binary, "-hide_banner", "-nostats",
                 "-i", str(wav_path),
                 "-af", "ebur128=peak=none", "-f", "null", "-"],
                capture_output=True, text=True,
            )
        text = proc.stderr
        return self._parse(text)

    @staticmethod
    def _parse(text: str) -> FfmpegSummary:
        # ffmpeg prints per-frame running values before the final summary; only
        # the "Summary:" block holds the gated result, so slice it out first.
        summary = text[text.rfind("Summary:"):] if "Summary:" in text else text

        def grab(pattern: str, flags: int = 0) -> float | None:
            m = re.search(pattern, summary, flags)
            if not m:
                return None
            val = m.group(1)
            if val in ("-inf", "inf"):
                return None
            return float(val)

        return FfmpegSummary(
            available=True,
            integrated_lufs=grab(r"I:\s*(-?\d+(?:\.\d+)?|-inf)\s*LUFS"),
            integrated_threshold_lufs=grab(
                r"Integrated loudness:.*?Threshold:\s*(-?\d+(?:\.\d+)?)",
                re.DOTALL,
            ),
            lra_lu=grab(r"LRA:\s*(-?\d+(?:\.\d+)?)\s*LU"),
            lra_threshold_lufs=grab(
                r"Loudness range:.*?Threshold:\s*(-?\d+(?:\.\d+)?)\s*LUFS",
                re.DOTALL,
            ),
            lra_low_lufs=grab(r"LRA low:\s*(-?\d+(?:\.\d+)?)\s*LUFS"),
            lra_high_lufs=grab(r"LRA high:\s*(-?\d+(?:\.\d+)?)\s*LUFS"),
            raw=text,
        )


class PyloudnormReference:
    """Integrated-loudness-only independent reference."""

    def __init__(self):
        import pyloudnorm  # local import: dev dependency only
        self._pyln = pyloudnorm

    def integrated_loudness(self, samples: np.ndarray) -> float:
        meter = self._pyln.Meter(48000)
        x = samples if samples.ndim == 2 else samples[:, None]
        return float(meter.integrated_loudness(x))

    def kweight_offline(self, samples: np.ndarray) -> np.ndarray:
        """Replicate K-weighting with pyloudnorm's *normative* DeMan design.

        Used to cross-check the project's streaming filter coefficients against
        an independent analogue-prototype design (not its default RBJ filters).
        """
        import pyloudnorm
        meter = pyloudnorm.Meter(48000, filter_class="DeMan")
        data = samples.copy()
        if data.ndim == 1:
            data = data[:, None]
        for stage in meter._filters.values():
            for c in range(data.shape[1]):
                data[:, c] = stage.apply_filter(data[:, c])
        return data


def db_from_energy(energy: float) -> float:
    return -0.691 + 10.0 * math.log10(energy)
