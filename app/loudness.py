"""Time/signal kernel: streaming, chunked EBU R128 loudness and LRA meter.

Processing order (every step mirrors the standards cited in
:mod:`app.r128_constants`)::

    decoded PCM (float64, per-chunk)
      -> K-weighting per channel: shelf, then RLB high-pass (state carried)
      -> split into 100 ms frames; trailing <100 ms samples are buffered
      -> per-frame weighted mean-square energy with BS.1770 channel weights
      -> 400 ms integrated blocks, 75% overlap  (one per completed frame)
      -> 3.0 s LRA blocks on a 1 s cadence       (2/3 overlap, Tech 3342)
      -> absolute gate (-70 LUFS) -> relative gate (-10 LU, integrated)
      -> LRA: absolute gate, relative gate at gated-STL-mean - 20 LU,
              0.1 LU histogram, 10th/95th percentile, no in-bin interpolation

A single feed of N bytes and many feeds of N bytes in arbitrary chunk sizes
produce identical statistics (the filters carry state and sub-frame input is
buffered); see tests/test_streaming_parity.py.

True-peak is intentionally absent. There is no oversampling stage here, so the
result reports ``true_peak = NOT_MEASURED`` and must never be quoted as a TP.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .errors import InvalidLayoutError, UnsupportedLayoutError
from .filters import StreamingKWeighting
from .r128_constants import (
    ABSOLUTE_GATE_LUFS,
    DEFAULT_LAYOUTS,
    HOP_SAMPLES,
    INTEGRATED_BLOCK_FRAMES,
    KERNEL_VERSION,
    LOUDNESS_OFFSET_DB,
    LRA_BLOCK_FRAMES,
    LRA_HOP_FRAMES,
    LRA_PERCENTILE_HIGH,
    LRA_PERCENTILE_LOW,
    LRA_RELATIVE_GATE_OFFSET_LU,
    ROLE_WEIGHTS,
    SPEC_ID,
    STATUS_INSUFFICIENT_BLOCKS,
    STATUS_NOT_COMPUTED,
    STATUS_OK,
    STATUS_SILENCE,
    TRUE_PEAK_NOT_MEASURED,
)


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------
@dataclass
class GatingSummary:
    """Block counts and thresholds around the two gate stages."""

    blocks_total: int
    blocks_above_absolute: int
    blocks_above_relative: int
    absolute_gate_lufs: float
    relative_gate_lufs: float | None
    ungated_mean_lufs: float | None          # mean energy of ALL blocks -> LUFS
    absolute_mean_lufs: float | None         # mean of blocks above abs gate


@dataclass
class LRASummary:
    status: str
    loudness_range_lu: float | None
    blocks_total: int
    blocks_above_absolute: int
    blocks_above_relative: int
    relative_gate_lufs: float | None
    percentile_10_lufs: float | None
    percentile_95_lufs: float | None
    histogram: dict[str, int]                # "loudness_bin_LUFS": count (sparse)


@dataclass
class SignalInfo:
    frames: int
    channels: int
    duration_seconds: float
    sample_rate_hz: int
    sample_peak: float | None                # max |sample| across channels
    true_peak: str = TRUE_PEAK_NOT_MEASURED
    trailing_samples_dropped: int = 0


@dataclass
class LoudnessResult:
    status: str
    integrated_loudness_lufs: float | None
    gating: GatingSummary
    lra: LRASummary
    signal: SignalInfo
    channel_layout: list[str]
    channel_weights: list[float]
    warnings: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    kernel_version: str = KERNEL_VERSION
    spec: str = SPEC_ID


# ---------------------------------------------------------------------------
# The streaming meter
# ---------------------------------------------------------------------------
class StreamingLoudnessMeter:
    """Feed PCM chunks in any size; read :meth:`finalize` at end of stream."""

    def __init__(self, channels: int, roles: list[str] | None = None):
        if channels < 1:
            raise UnsupportedLayoutError(
                "channel count must be >= 1", details={"channels": channels})
        if roles is None:
            if channels not in DEFAULT_LAYOUTS:
                raise UnsupportedLayoutError(
                    f"{channels} channels have no built-in default layout; "
                    "supply explicit roles",
                    details={"channels": channels,
                             "default_counts": sorted(DEFAULT_LAYOUTS)},
                )
            roles = list(DEFAULT_LAYOUTS[channels])
        if len(roles) != channels:
            raise InvalidLayoutError(
                f"roles length {len(roles)} does not match channel count "
                f"{channels}",
                details={"roles": roles, "channels": channels},
            )
        unknown = [r for r in roles if r not in ROLE_WEIGHTS and r != "LFE"]
        if unknown:
            raise UnsupportedLayoutError(
                f"unknown channel role(s): {unknown}",
                details={"unknown": unknown},
            )
        self.channels = channels
        self.roles = list(roles)
        # LFE weight is 0 (absent from ROLE_WEIGHTS); every other role weighted.
        self.weights = np.array(
            [ROLE_WEIGHTS.get(r, 0.0) for r in roles], dtype=np.float64
        )

        self._kfilter = StreamingKWeighting(channels)

        # sub-frame accumulation
        self._carry = np.empty((0, channels), dtype=np.float64)
        self._frames_seen = 0
        self._total_samples_in = 0
        self._sample_peak = 0.0
        self._all_abs_samples_zero = True

        # integrated ring: last INTEGRATED_BLOCK_FRAMES frame energy vectors
        self._frame_ring = np.zeros(
            (INTEGRATED_BLOCK_FRAMES, channels), dtype=np.float64
        )
        self._int_block_energies: list[np.ndarray] = []  # per-channel energy

        # LRA: on each 10th frame store the mean per-channel energy of the last
        # 30 frames; keep a rolling ring of 30 frame energies.
        self._lra_ring = np.zeros((LRA_BLOCK_FRAMES, channels), dtype=np.float64)
        self._lra_block_energies: list[np.ndarray] = []

    # -- input ------------------------------------------------------------
    def push(self, samples: np.ndarray) -> None:
        """Append a ``(frames, channels)`` float64 decoded chunk."""
        samples = np.asarray(samples, dtype=np.float64)
        if samples.size == 0:
            return
        if samples.ndim != 2 or samples.shape[1] != self.channels:
            raise ValueError(
                f"chunk must be (frames, {self.channels}); got {samples.shape}"
            )
        # non-finite input can never yield a defensible measurement
        if not np.all(np.isfinite(samples)):
            raise ValueError("non-finite samples (NaN/Inf) in input")

        self._total_samples_in += samples.shape[0]
        peak = float(np.max(np.abs(samples))) if samples.size else 0.0
        if peak > self._sample_peak:
            self._sample_peak = peak
        if not np.all(samples == 0.0):
            self._all_abs_samples_zero = False

        weighted = self._kfilter.process(samples)

        if self._carry.shape[0]:
            weighted = np.concatenate([self._carry, weighted], axis=0)
        n_frames = weighted.shape[0] // HOP_SAMPLES
        if n_frames:
            usable = n_frames * HOP_SAMPLES
            frames = weighted[:usable].reshape(
                n_frames, HOP_SAMPLES, self.channels
            )
            self._carry = weighted[usable:]
            # mean-square energy per channel per 100 ms frame
            frame_energy = np.mean(frames * frames, axis=1)  # (n_frames, ch)
            self._ingest_frames(frame_energy)
            self._frames_seen += n_frames
        else:
            self._carry = weighted

    def _ingest_frames(self, fe: np.ndarray) -> None:
        for i in range(fe.shape[0]):
            e = fe[i]
            # integrated: roll a 4-frame window
            self._frame_ring = np.roll(self._frame_ring, -1, axis=0)
            self._frame_ring[-1] = e
            if self._frames_seen + i + 1 >= INTEGRATED_BLOCK_FRAMES:
                # mean over the 4 frames -> same scale as a 400 ms block energy
                self._int_block_energies.append(self._frame_ring.mean(axis=0))
            # LRA: roll a 30-frame window; record on frames 30,40,50,...
            self._lra_ring = np.roll(self._lra_ring, -1, axis=0)
            self._lra_ring[-1] = e
            frame_index = self._frames_seen + i + 1  # 1-based count so far
            if frame_index >= LRA_BLOCK_FRAMES and \
                    (frame_index - LRA_BLOCK_FRAMES) % LRA_HOP_FRAMES == 0:
                self._lra_block_energies.append(self._lra_ring.mean(axis=0))

    # -- finalize ---------------------------------------------------------
    def finalize(self) -> LoudnessResult:
        warnings: list[str] = []
        trailing = self._carry.shape[0]
        if trailing:
            warnings.append(
                f"{trailing} trailing samples (< {HOP_SAMPLES}-sample / "
                "100 ms frame) were not part of any complete block and were "
                "excluded from the statistics"
            )

        int_loudness, int_status, gating = self._integrated()
        lra = self._loudness_range()

        # Integrated loudness may be valid while LRA has fewer than 3 s of
        # material; that is a partial result, not a failed job. The job takes
        # the integrated status and the LRA sub-status carries the detail.
        status = int_status

        signal = SignalInfo(
            frames=self._frames_seen * HOP_SAMPLES,
            channels=self.channels,
            duration_seconds=self._total_samples_in / 48000.0,
            sample_rate_hz=48000,
            sample_peak=self._sample_peak if self._total_samples_in else None,
            trailing_samples_dropped=trailing,
        )
        return LoudnessResult(
            status=status,
            integrated_loudness_lufs=int_loudness,
            gating=gating,
            lra=lra,
            signal=signal,
            channel_layout=list(self.roles),
            channel_weights=list(self.weights),
            warnings=warnings,
            uncertainties=[
                "true-peak level was not measured (no oversampling); "
                "true_peak=NOT_MEASURED"
            ],
        )

    # -- integrated loudness ---------------------------------------------
    @staticmethod
    def _weighted_sum(block_energy: np.ndarray, weights: np.ndarray) -> float:
        return float(np.sum(weights * block_energy))

    @staticmethod
    def _to_lufs(energy: float) -> float:
        return LOUDNESS_OFFSET_DB + 10.0 * math.log10(energy)

    def _integrated(self):
        energies = self._int_block_energies
        total = len(energies)
        abs_gate_energy = 10.0 ** (
            (ABSOLUTE_GATE_LUFS - LOUDNESS_OFFSET_DB) / 10.0
        )

        def mean_lufs(blocks: list[np.ndarray]) -> float | None:
            if not blocks:
                return None
            mean_per_ch = np.mean(np.stack(blocks, axis=0), axis=0)
            e = self._weighted_sum(mean_per_ch, self.weights)
            if e <= 0.0:
                return None
            return self._to_lufs(e)

        ungated = mean_lufs(energies)

        if total == 0:
            # No complete 400 ms block at all.
            status = STATUS_SILENCE if self._all_abs_samples_zero \
                else STATUS_INSUFFICIENT_BLOCKS
            gating = GatingSummary(
                blocks_total=0,
                blocks_above_absolute=0,
                blocks_above_relative=0,
                absolute_gate_lufs=ABSOLUTE_GATE_LUFS,
                relative_gate_lufs=None,
                ungated_mean_lufs=None,
                absolute_mean_lufs=None,
            )
            return None, status, gating

        weighted = [self._weighted_sum(e, self.weights) for e in energies]
        above_abs_idx = [i for i, e in enumerate(weighted) if e >= abs_gate_energy]
        above_abs_blocks = [energies[i] for i in above_abs_idx]
        absolute_mean = mean_lufs(above_abs_blocks)

        if not above_abs_idx:
            # Every complete block sits below -70 LUFS.
            status = STATUS_SILENCE if self._all_abs_samples_zero \
                else STATUS_NOT_COMPUTED
            gating = GatingSummary(
                blocks_total=total,
                blocks_above_absolute=0,
                blocks_above_relative=0,
                absolute_gate_lufs=ABSOLUTE_GATE_LUFS,
                relative_gate_lufs=None,
                ungated_mean_lufs=ungated,
                absolute_mean_lufs=None,
            )
            return None, status, gating

        # Relative threshold from the absolute-gated mean energy (BS.1770 eq.6).
        abs_mean_energy = self._weighted_sum(
            np.mean(np.stack(above_abs_blocks, axis=0), axis=0), self.weights
        )
        relative_gate = self._to_lufs(abs_mean_energy) - 10.0
        rel_gate_energy = 10.0 ** (
            (relative_gate - LOUDNESS_OFFSET_DB) / 10.0
        )
        # BS.1770-4 eq.7: strictly greater than BOTH thresholds. Absolute is
        # numerically redundant here but applied explicitly per the standard.
        final_idx = [
            i for i, e in enumerate(weighted)
            if e > rel_gate_energy and e > abs_gate_energy
        ]
        final_blocks = [energies[i] for i in final_idx]
        gating = GatingSummary(
            blocks_total=total,
            blocks_above_absolute=len(above_abs_idx),
            blocks_above_relative=len(final_idx),
            absolute_gate_lufs=ABSOLUTE_GATE_LUFS,
            relative_gate_lufs=relative_gate,
            ungated_mean_lufs=ungated,
            absolute_mean_lufs=absolute_mean,
        )
        if not final_blocks:  # cannot happen mathematically, but do not invent
            return None, STATUS_NOT_COMPUTED, gating

        gated_mean = np.mean(np.stack(final_blocks, axis=0), axis=0)
        integrated = self._to_lufs(self._weighted_sum(gated_mean, self.weights))
        return integrated, STATUS_OK, gating

    # -- loudness range ---------------------------------------------------
    def _zero_lra(self, total: int, above_abs: int,
                  rel_gate: float | None) -> LRASummary:
        return LRASummary(
            status=STATUS_NOT_COMPUTED if total else STATUS_INSUFFICIENT_BLOCKS,
            loudness_range_lu=None,
            blocks_total=total,
            blocks_above_absolute=above_abs,
            blocks_above_relative=0,
            relative_gate_lufs=rel_gate,
            percentile_10_lufs=None,
            percentile_95_lufs=None,
            histogram={},
        )

    def _loudness_range(self) -> LRASummary:
        blocks = self._lra_block_energies
        total = len(blocks)
        if total == 0:
            # Need 3.0 s of signal to form one block.
            status = STATUS_SILENCE if self._all_abs_samples_zero \
                else STATUS_INSUFFICIENT_BLOCKS
            z = self._zero_lra(0, 0, None)
            z.status = status
            return z

        weighted = [self._weighted_sum(e, self.weights) for e in blocks]
        # absolute gate at -70 LUFS (bin boundary 0 in the ffmpeg histogram)
        abs_gate_energy = 10.0 ** (
            (ABSOLUTE_GATE_LUFS - LOUDNESS_OFFSET_DB) / 10.0
        )
        abs_energies = [e for e in weighted if e >= abs_gate_energy]
        if not abs_energies:
            z = self._zero_lra(total, 0, None)
            z.status = STATUS_SILENCE if self._all_abs_samples_zero \
                else STATUS_NOT_COMPUTED
            return z

        # relative gate: mean energy of abs-gated 3 s blocks, minus 20 LU.
        stl_mean_energy = float(np.mean(abs_energies))
        rel_gate_energy = stl_mean_energy * (10.0 ** (
            LRA_RELATIVE_GATE_OFFSET_LU / 10.0
        ))
        rel_gate_lufs = self._to_lufs(rel_gate_energy)

        # Build the 0.1 LU histogram from ALL abs-gated blocks. Bin i spans
        # [-70 + 0.1 i, -70 + 0.1(i+1)); its representative loudness is
        # -69.95 + 0.1 i (ffmpeg histogram_energies).
        hist: dict[int, int] = {}
        for e in abs_energies:
            loud = self._to_lufs(e)
            idx = int(math.floor(10.0 * (loud + 70.0)))
            idx = max(0, min(idx, 999))  # documented 1000-bin table, 0..999
            hist[idx] = hist.get(idx, 0) + 1

        # Relative gate is applied to bin REPRESENTATIVES: counting starts at
        # the first bin whose representative is at/above the gate loudness.
        first_kept_idx = max(0, int(math.ceil((rel_gate_lufs + 69.95) / 0.1)))
        kept = {i: c for i, c in hist.items() if i >= first_kept_idx}
        n_gated = sum(kept.values())
        if n_gated == 0:
            z = self._zero_lra(total, len(abs_energies), rel_gate_lufs)
            z.status = STATUS_NOT_COMPUTED
            return z

        # ffmpeg ranks with half-up rounding of zero-based positions and walks
        # the histogram cumulatively, using each bin's representative, with no
        # within-bin interpolation.
        rank_lo = int(math.floor((n_gated - 1) * LRA_PERCENTILE_LOW + 0.5))
        rank_hi = int(math.floor((n_gated - 1) * LRA_PERCENTILE_HIGH + 0.5))

        cumulative = 0
        j = min(kept)
        low_idx = high_idx = j
        while cumulative <= rank_lo:
            cumulative += kept.get(j, 0)
            low_idx = j
            j += 1
        while cumulative <= rank_hi:
            cumulative += kept.get(j, 0)
            high_idx = j
            j += 1

        p10 = -69.95 + 0.1 * low_idx
        p95 = -69.95 + 0.1 * high_idx
        lra_value = round((high_idx - low_idx) * 0.1, 2)

        sparse_hist = {
            f"{(-69.95 + 0.1 * i):.1f}": kept[i] for i in sorted(kept)
        }
        return LRASummary(
            status=STATUS_OK,
            loudness_range_lu=lra_value,
            blocks_total=total,
            blocks_above_absolute=len(abs_energies),
            blocks_above_relative=n_gated,
            relative_gate_lufs=rel_gate_lufs,
            percentile_10_lufs=round(p10, 2),
            percentile_95_lufs=round(p95, 2),
            histogram=sparse_hist,
        )


def analyze_array(samples: np.ndarray,
                  roles: list[str] | None = None) -> LoudnessResult:
    """Convenience: measure a complete in-memory ``(frames, channels)`` array.

    Routes through exactly the same streaming code path as chunked jobs by
    pushing the array once, so there is no second implementation to keep in
    sync.
    """
    samples = np.asarray(samples, dtype=np.float64)
    if samples.ndim == 1:
        samples = samples[:, None]
    meter = StreamingLoudnessMeter(samples.shape[1], roles=roles)
    meter.push(samples)
    return meter.finalize()
