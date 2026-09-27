"""Time/signal core for EBU R128 integrated loudness and loudness range.

Measurement pipeline (order fixed by EBU Tech 3341/3342 + BS.1770-4):

  1. K-weighting (high-shelf then high-pass), zero rest state      [filter.py]
  2. Channel weighting: per-channel squared energy weighted by G   (here)
  3. Mean-square energy of overlapping gating blocks:
       - momentary: T_g = 0.400 s, hop = 0.100 s  (integrated loudness)
       - short-term: T_g = 3.000 s, hop = 0.100 s (LRA)
  4. Block loudness: L_j = -0.691 + 10*log10(sum_c G_c * z_cj)
  5. INTEGRATED gating (Tech 3341):
       a. absolute gate at -70 LUFS;
       b. ungated loudness of blocks above the absolute gate;
       c. relative gate = that loudness - 10 LU;
       d. integrated loudness from blocks above BOTH gates
         (energy mean of selected blocks, NOT a mean of dB values and
          NOT plain RMS).
  6. LRA gating (Tech 3342) on short-term blocks:
       a. absolute gate at -70 LUFS;
       b. relative gate = integrated loudness (of abs-gated short-term
          blocks) - 20 LU;
       c. LRA = P95 - P10 of the remaining block loudness values.

Streaming contract: :class:`StreamingMeter` accepts arbitrary chunk sizes; the
emitted blocks and final statistics are identical to feeding the whole signal
at once. Only complete blocks are emitted (the trailing partial window is
discarded, per the standard) — which also defines the silence/short-signal
statuses.

True-peak is NOT measured here (no oversampling meter is implemented), so no
true-peak value is ever claimed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .filter import StreamingKWeighting

# BS.1770-4 offset constant (LKFS/LUFS).
LOUDNESS_OFFSET = -0.691

# Channel gains per BS.1770-4, indexed by the selected analysis-channel order
# [Left, Right, Center, Left surround, Right surround]. 1.0 = 0 dB,
# 1.41 ~= +3.01 dB for surrounds. The LFE channel is never present here (it is
# removed at the media-parsing layer).
CHANNEL_GAINS_BS1770_4 = (1.0, 1.0, 1.0, 1.41, 1.41)

# Result statuses.
STATUS_OK = "OK"
STATUS_OK_WITH_WARNINGS = "OK_WITH_WARNINGS"
STATUS_SILENCE = "SILENCE"
STATUS_INSUFFICIENT_BLOCKS = "INSUFFICIENT_BLOCKS"
LRA_NOT_APPLICABLE = "NOT_APPLICABLE"


class _BlockEmitter:
    """Emits fixed-length, fixed-hop overlapping blocks from streamed audio."""

    def __init__(self, block_samples: int, hop_samples: int, num_channels: int):
        self.block_samples = block_samples
        self.hop_samples = hop_samples
        self.num_channels = num_channels
        self._buffer = np.empty((0, num_channels), dtype=np.float64)

    def push(self, filtered: np.ndarray) -> list[np.ndarray]:
        self._buffer = (filtered if self._buffer.shape[0] == 0
                        else np.concatenate((self._buffer, filtered), axis=0))
        blocks: list[np.ndarray] = []
        while self._buffer.shape[0] >= self.block_samples:
            blocks.append(self._buffer[:self.block_samples].copy())
            self._buffer = self._buffer[self.hop_samples:]
        return blocks

    @property
    def dropped_tail_samples(self) -> int:
        """Samples held back because they do not fill one more full block."""
        return self._buffer.shape[0]


@dataclass
class GateStats:
    """Intermediate statistics around one gating stage (auditable output)."""
    total_blocks: int
    above_absolute_gate: int
    above_both_gates: int
    absolute_gate_lufs: float
    relative_gate_lufs: float | None
    # Energy-domain mean of the selected blocks (sum_c G_c * mean_j z_cj).
    selected_mean_power: float | None

    def to_dict(self) -> dict:
        return {
            "total_blocks": self.total_blocks,
            "above_absolute_gate": self.above_absolute_gate,
            "above_both_gates": self.above_both_gates,
            "absolute_gate_lufs": self.absolute_gate_lufs,
            "relative_gate_lufs": self.relative_gate_lufs,
            "selected_mean_power": self.selected_mean_power,
        }


@dataclass
class IntegratedResult:
    status: str
    integrated_lufs: float | None
    gate: GateStats
    momentary_block_loudness: list[float] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class LRAResult:
    status: str
    lra_lu: float | None
    gate: GateStats
    percentile_low_lufs: float | None
    percentile_high_lufs: float | None
    short_term_block_loudness: list[float] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def block_loudness(block: np.ndarray, gains: np.ndarray) -> float:
    """Mean-square energy per channel then weighted sum, BS.1770 eq. (1)/(4)."""
    block_len = block.shape[0]
    # z_c = 1/T * sum y^2 for each channel
    z = np.sum(block * block, axis=0) / block_len
    power = float(np.sum(gains * z))
    if power <= 0.0:
        return float("-inf")
    return LOUDNESS_OFFSET + 10.0 * np.log10(power)


class StreamingMeter:
    """Chunked R128 meter. Push K-weighted or raw PCM chunks, then finalize.

    ``weights`` selects the analysis channels and their BS.1770 gains; the
    input samples must already be ordered to match (LFE removed upstream).
    """

    def __init__(self,
                 sample_rate: int,
                 channel_weights: tuple[float, ...],
                 momentary_block_sec: float = 0.4,
                 momentary_hop_sec: float = 0.1,
                 shortterm_block_sec: float = 3.0,
                 shortterm_hop_sec: float = 0.1,
                 absolute_gate_lufs: float = -70.0,
                 integrated_relative_offset_lu: float = -10.0,
                 lra_relative_offset_lu: float = -20.0,
                 lra_confident_block_count: int = 30,
                 apply_k_weighting: bool = True):
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if not channel_weights:
            raise ValueError("at least one analysis channel is required")
        self.sample_rate = sample_rate
        self.num_channels = len(channel_weights)
        self.weights = np.asarray(channel_weights, dtype=np.float64)

        self.momentary_block_samples = int(round(momentary_block_sec * sample_rate))
        self.momentary_hop_samples = int(round(momentary_hop_sec * sample_rate))
        self.shortterm_block_samples = int(round(shortterm_block_sec * sample_rate))
        self.shortterm_hop_samples = int(round(shortterm_hop_sec * sample_rate))

        self.absolute_gate_lufs = absolute_gate_lufs
        self.integrated_relative_offset_lu = integrated_relative_offset_lu
        self.lra_relative_offset_lu = lra_relative_offset_lu
        self.lra_confident_block_count = lra_confident_block_count

        self._kweight = (StreamingKWeighting(sample_rate, self.num_channels)
                         if apply_k_weighting else None)
        self._momentary = _BlockEmitter(self.momentary_block_samples,
                                        self.momentary_hop_samples,
                                        self.num_channels)
        self._shortterm = _BlockEmitter(self.shortterm_block_samples,
                                        self.shortterm_hop_samples,
                                        self.num_channels)
        self._momentary_ms: list[np.ndarray] = []
        self._shortterm_ms: list[np.ndarray] = []
        self._samples_seen = 0

    def push(self, samples: np.ndarray) -> None:
        """Push one PCM chunk, shape (n, num_analysis_channels), float64."""
        if samples.ndim != 2 or samples.shape[1] != self.num_channels:
            raise ValueError(
                f"expected shape (n, {self.num_channels}), got {samples.shape}")
        x = np.asarray(samples, dtype=np.float64)
        if x.shape[0] == 0:
            return
        filtered = self._kweight.process(x) if self._kweight is not None else x
        self._samples_seen += filtered.shape[0]
        for blk in self._momentary.push(filtered):
            self._momentary_ms.append(self._block_channel_ms(blk))
        for blk in self._shortterm.push(filtered):
            self._shortterm_ms.append(self._block_channel_ms(blk))

    def _block_channel_ms(self, block: np.ndarray) -> np.ndarray:
        """Per-channel mean-square energy z_c of one block."""
        return np.sum(block * block, axis=0) / block.shape[0]

    # -- loudness helpers -------------------------------------------------

    def _loudness_from_ms(self, z: np.ndarray) -> float:
        power = float(np.sum(self.weights * z))
        if power <= 0.0:
            return float("-inf")
        return LOUDNESS_OFFSET + 10.0 * np.log10(power)

    def _loudness_list(self, block_ms: list[np.ndarray]) -> np.ndarray:
        return np.asarray([self._loudness_from_ms(z) for z in block_ms],
                          dtype=np.float64)

    # -- finalization -----------------------------------------------------

    def finalize(self, include_blocks: bool = False) -> dict:
        momentary_loudness = self._loudness_list(self._momentary_ms)
        shortterm_loudness = self._loudness_list(self._shortterm_ms)
        duration_sec = self._samples_seen / self.sample_rate

        integrated = self._finalize_integrated(momentary_loudness)
        lra = self._finalize_lra(shortterm_loudness, integrated)

        return {
            "duration_sec": duration_sec,
            "samples_seen": self._samples_seen,
            "sample_rate": self.sample_rate,
            "num_analysis_channels": self.num_channels,
            "momentary": {
                "block_sec": self.momentary_block_samples / self.sample_rate,
                "hop_sec": self.momentary_hop_samples / self.sample_rate,
                "dropped_tail_samples": self._momentary.dropped_tail_samples,
            },
            "short_term": {
                "block_sec": self.shortterm_block_samples / self.sample_rate,
                "hop_sec": self.shortterm_hop_samples / self.sample_rate,
                "dropped_tail_samples": self._shortterm.dropped_tail_samples,
            },
            "integrated": self._integrated_to_dict(integrated, include_blocks,
                                                   momentary_loudness),
            "lra": self._lra_to_dict(lra, include_blocks, shortterm_loudness),
            "true_peak": None,
            "true_peak_supported": False,
        }

    def _finalize_integrated(self, loudness: np.ndarray) -> IntegratedResult:
        total = loudness.shape[0]
        if total == 0:
            # Shorter than one 400 ms gating block: R128 gives no number.
            return IntegratedResult(
                status=STATUS_INSUFFICIENT_BLOCKS,
                integrated_lufs=None,
                gate=GateStats(0, 0, 0, self.absolute_gate_lufs, None, None),
            )

        abs_mask = loudness >= self.absolute_gate_lufs
        n_abs = int(np.count_nonzero(abs_mask))
        if n_abs == 0:
            return IntegratedResult(
                status=STATUS_SILENCE,
                integrated_lufs=None,
                gate=GateStats(total, 0, 0, self.absolute_gate_lufs, None, 0.0),
            )

        # Ungated (absolute-only) loudness from mean block energy per channel.
        z_abs = self._mean_channel_ms([self._momentary_ms[i]
                                       for i in np.flatnonzero(abs_mask)])
        ungated = self._loudness_from_ms(z_abs)
        relative_gate = ungated + self.integrated_relative_offset_lu

        both_mask = abs_mask & (loudness > relative_gate)
        n_both = int(np.count_nonzero(both_mask))
        if n_both == 0:
            # Only possible for pathological distributions at the boundary;
            # fall back to the absolute-gated result and flag it.
            mean_power = float(np.sum(self.weights * z_abs))
            return IntegratedResult(
                status=STATUS_OK_WITH_WARNINGS,
                integrated_lufs=ungated,
                gate=GateStats(total, n_abs, 0, self.absolute_gate_lufs,
                               relative_gate, mean_power),
                warnings=["RELATIVE_GATE_EMPTY_USED_ABSOLUTE_ONLY"],
            )

        z_sel = self._mean_channel_ms([self._momentary_ms[i]
                                       for i in np.flatnonzero(both_mask)])
        mean_power = float(np.sum(self.weights * z_sel))
        integrated_lufs = LOUDNESS_OFFSET + 10.0 * np.log10(mean_power)
        return IntegratedResult(
            status=STATUS_OK,
            integrated_lufs=integrated_lufs,
            gate=GateStats(total, n_abs, n_both, self.absolute_gate_lufs,
                           relative_gate, mean_power),
        )

    def _finalize_lra(self, shortterm_loudness: np.ndarray,
                      integrated: IntegratedResult) -> LRAResult:
        total = shortterm_loudness.shape[0]
        if total == 0:
            status = STATUS_INSUFFICIENT_BLOCKS
            if integrated.status == STATUS_SILENCE:
                status = STATUS_SILENCE
            return LRAResult(
                status=status, lra_lu=None,
                gate=GateStats(0, 0, 0, self.absolute_gate_lufs, None, None),
                percentile_low_lufs=None, percentile_high_lufs=None,
            )

        abs_mask = shortterm_loudness >= self.absolute_gate_lufs
        n_abs = int(np.count_nonzero(abs_mask))
        if n_abs == 0:
            return LRAResult(
                status=STATUS_SILENCE, lra_lu=None,
                gate=GateStats(total, 0, 0, self.absolute_gate_lufs, None, 0.0),
                percentile_low_lufs=None, percentile_high_lufs=None,
            )

        # Tech 3342 relative gate: integrated loudness of the abs-gated
        # SHORT-TERM blocks, minus 20 LU (independent of program gated M).
        z_abs = self._mean_channel_ms([self._shortterm_ms[i]
                                       for i in np.flatnonzero(abs_mask)])
        st_integrated = self._loudness_from_ms(z_abs)
        relative_gate = st_integrated + self.lra_relative_offset_lu

        rel_mask = abs_mask & (shortterm_loudness > relative_gate)
        n_rel = int(np.count_nonzero(rel_mask))
        if n_rel == 0:
            return LRAResult(
                status=STATUS_SILENCE, lra_lu=None,
                gate=GateStats(total, n_abs, 0, self.absolute_gate_lufs,
                               relative_gate, 0.0),
                percentile_low_lufs=None, percentile_high_lufs=None,
            )

        selected = np.sort(shortterm_loudness[rel_mask])
        # Linear-interpolated quantiles, matching numpy default and the common
        # EBU histogram interpolation closely; documented trade-off in README.
        p_low = float(np.percentile(selected, 10))
        p_high = float(np.percentile(selected, 95))
        lra = p_high - p_low
        mean_power = float(np.sum(self.weights * z_abs))

        warnings: list[str] = []
        status = STATUS_OK
        if total < self.lra_confident_block_count:
            status = STATUS_OK_WITH_WARNINGS
            warnings.append("LRA_LOW_CONFIDENCE_FEW_SHORTTERM_BLOCKS")
        return LRAResult(
            status=status, lra_lu=float(lra),
            gate=GateStats(total, n_abs, n_rel, self.absolute_gate_lufs,
                           relative_gate, mean_power),
            percentile_low_lufs=p_low, percentile_high_lufs=p_high,
            warnings=warnings,
        )

    def _mean_channel_ms(self, block_ms: list[np.ndarray]) -> np.ndarray:
        return np.mean(np.stack(block_ms, axis=0), axis=0)

    @staticmethod
    def _integrated_to_dict(result: IntegratedResult, include_blocks: bool,
                            loudness: np.ndarray) -> dict:
        out = {
            "status": result.status,
            "integrated_lufs": result.integrated_lufs,
            "warnings": result.warnings,
            "gate_stats": result.gate.to_dict(),
        }
        if include_blocks:
            out["block_loudness_lufs"] = [
                None if np.isneginf(x) else float(x) for x in loudness]
        return out

    @staticmethod
    def _lra_to_dict(result: LRAResult, include_blocks: bool,
                     loudness: np.ndarray) -> dict:
        out = {
            "status": result.status,
            "lra_lu": result.lra_lu,
            "warnings": result.warnings,
            "percentile_p10_lufs": result.percentile_low_lufs,
            "percentile_p95_lufs": result.percentile_high_lufs,
            "gate_stats": result.gate.to_dict(),
        }
        if include_blocks:
            out["block_loudness_lufs"] = [
                None if np.isneginf(x) else float(x) for x in loudness]
        return out
