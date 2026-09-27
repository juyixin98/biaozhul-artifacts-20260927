"""Measurement orchestration: parsed audio -> StreamingMeter -> result envelope.

This layer is shared by the synchronous WAV endpoint and the job worker, so
there is exactly one execution path for the math.
"""

from __future__ import annotations

import time

from .config import Settings
from .kernel.meter import (
    STATUS_INSUFFICIENT_BLOCKS,
    STATUS_OK,
    STATUS_OK_WITH_WARNINGS,
    STATUS_SILENCE,
    StreamingMeter,
)
from .media import DecodedAudio

# Aggregate top-level status: silence/short inputs surface distinctly even if
# the other sub-measure (integrated vs LRA) succeeded.
STATUS_INTEGRATED_ONLY = "INTEGRATED_OK_LRA_NOT_APPLICABLE"


def measure(decoded: DecodedAudio,
            *,
            request_id: str,
            settings: Settings,
            include_blocks: bool = False,
            chunk_samples: int | None = None,
            label: str | None = None) -> dict:
    """Run the full R128 measurement.

    ``chunk_samples`` forces streaming in fixed-size chunks; results must be
    identical regardless of its value (exercised by the chunk-consistency
    tests). ``None`` feeds the whole signal at once.
    """
    started = time.perf_counter()
    meter = StreamingMeter(
        sample_rate=decoded.sample_rate,
        channel_weights=decoded.channel_weights,
        momentary_block_sec=settings.momentary_block_sec,
        momentary_hop_sec=settings.momentary_hop_sec,
        shortterm_block_sec=settings.shortterm_block_sec,
        shortterm_hop_sec=settings.shortterm_hop_sec,
        absolute_gate_lufs=settings.absolute_gate_lufs,
        integrated_relative_offset_lu=settings.integrated_relative_offset_lu,
        lra_relative_offset_lu=settings.lra_relative_offset_lu,
        lra_confident_block_count=settings.lra_confident_block_count,
    )

    samples = decoded.samples
    if chunk_samples is None:
        meter.push(samples)
    else:
        for start in range(0, samples.shape[0], chunk_samples):
            meter.push(samples[start:start + chunk_samples])

    result = meter.finalize(include_blocks=include_blocks)
    processing_ms = round((time.perf_counter() - started) * 1000.0, 3)

    int_status = result["integrated"]["status"]
    lra_status = result["lra"]["status"]
    overall = _overall_status(int_status, lra_status)
    warnings = list(result["integrated"]["warnings"]) + list(result["lra"]["warnings"])
    # Uncertain conclusions are listed separately from hard failures.
    uncertainties = list(warnings)
    if result["true_peak"] is None:
        uncertainties.append("TRUE_PEAK_NOT_MEASURED_NOT_CLAIMED")

    return {
        "request_id": request_id,
        "label": label,
        "status": overall,
        "warnings": warnings,
        "uncertainties": uncertainties,
        "signal": {
            "sample_rate": decoded.sample_rate,
            "layout": decoded.layout,
            "source_channels": decoded.source_channels,
            "source_format": decoded.source_format,
            "analysis_channels": result["num_analysis_channels"],
            "channel_weights": list(decoded.channel_weights),
            "duration_sec": round(result["duration_sec"], 6),
            "samples_seen": result["samples_seen"],
        },
        "parameters": {
            "momentary_block_sec": result["momentary"]["block_sec"],
            "momentary_hop_sec": result["momentary"]["hop_sec"],
            "short_term_block_sec": result["short_term"]["block_sec"],
            "short_term_hop_sec": result["short_term"]["hop_sec"],
            "absolute_gate_lufs": settings.absolute_gate_lufs,
            "integrated_relative_gate_offset_lu": settings.integrated_relative_offset_lu,
            "lra_relative_gate_offset_lu": settings.lra_relative_offset_lu,
            "percentiles": "P10/P95 linear interpolation",
            "momentary_dropped_tail_samples": result["momentary"]["dropped_tail_samples"],
            "short_term_dropped_tail_samples": result["short_term"]["dropped_tail_samples"],
        },
        "integrated_loudness": result["integrated"],
        "loudness_range": result["lra"],
        "true_peak_tpfs": result["true_peak"],
        "true_peak_supported": result["true_peak_supported"],
        "provenance": {
            "algorithm_id": settings.algorithm_id,
            "spec_refs": list(settings.algorithm_spec_refs),
            "worker_id": settings.worker_id,
            "processing_ms": processing_ms,
            "streamed_chunk_samples": chunk_samples,
        },
    }


def _overall_status(int_status: str, lra_status: str) -> str:
    if int_status in (STATUS_SILENCE, STATUS_INSUFFICIENT_BLOCKS):
        return int_status
    if lra_status in (STATUS_SILENCE, STATUS_INSUFFICIENT_BLOCKS):
        return STATUS_INTEGRATED_ONLY
    if STATUS_OK_WITH_WARNINGS in (int_status, lra_status):
        return STATUS_OK_WITH_WARNINGS
    if int_status == STATUS_OK and lra_status == STATUS_OK:
        return STATUS_OK
    return STATUS_OK_WITH_WARNINGS
