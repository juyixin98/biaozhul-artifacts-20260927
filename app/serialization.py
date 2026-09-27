"""Conversion of kernel dataclasses to the JSON-schema response payload."""
from __future__ import annotations

from typing import Any

from .loudness import LoudnessResult


def result_to_dict(r: LoudnessResult) -> dict[str, Any]:
    return {
        "status": r.status,
        "integrated_loudness_lufs": r.integrated_loudness_lufs,
        "true_peak": r.signal.true_peak,
        "loudness_range": {
            "status": r.lra.status,
            "lra_lu": r.lra.loudness_range_lu,
            "blocks_total": r.lra.blocks_total,
            "blocks_above_absolute_gate": r.lra.blocks_above_absolute,
            "blocks_above_relative_gate": r.lra.blocks_above_relative,
            "relative_gate_lufs": r.lra.relative_gate_lufs,
            "percentile_10_lufs": r.lra.percentile_10_lufs,
            "percentile_95_lufs": r.lra.percentile_95_lufs,
            "gated_histogram_LUFS_rep_to_count": r.lra.histogram,
        },
        "integrated_gating": {
            "blocks_total": r.gating.blocks_total,
            "blocks_above_absolute_gate": r.gating.blocks_above_absolute,
            "blocks_above_relative_gate": r.gating.blocks_above_relative,
            "absolute_gate_lufs": r.gating.absolute_gate_lufs,
            "relative_gate_lufs": r.gating.relative_gate_lufs,
            "ungated_mean_lufs": r.gating.ungated_mean_lufs,
            "absolute_gated_mean_lufs": r.gating.absolute_mean_lufs,
        },
        "signal": {
            "frames_measured": r.signal.frames,
            "channels": r.signal.channels,
            "duration_seconds": round(r.signal.duration_seconds, 6),
            "sample_rate_hz": r.signal.sample_rate_hz,
            "sample_peak_linear": r.signal.sample_peak,
            "trailing_samples_dropped": r.signal.trailing_samples_dropped,
        },
        "channel_layout": r.channel_layout,
        "channel_weights_energy": r.channel_weights,
        "warnings": r.warnings,
        "uncertainties": r.uncertainties,
        "kernel_version": r.kernel_version,
        "method": {
            "specification": r.spec,
            "hop_ms": 100,
            "integrated_block_ms": 400,
            "integrated_overlap": "75%",
            "lra_block_s": 3.0,
            "lra_hop_s": 1.0,
            "lra_overlap": "2/3 (EBU Tech 3342 max overlap)",
            "absolute_gate_lufs": -70.0,
            "relative_gate_integrated_lu": -10.0,
            "relative_gate_lra_lu": -20.0,
            "lra_bin_lu": 0.1,
            "lra_percentiles": [10, 95],
            "channel_weight_surround_energy": 1.41,
            "channel_weight_dualmono_energy": 2.0,
            "k_weighting": "BS.1770-4 high-shelf then RLB high-pass @48k",
            "true_peak": "not measured (no oversampling stage present)",
        },
    }
