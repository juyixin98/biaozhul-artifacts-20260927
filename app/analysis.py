"""Result interpretation: comparison summary, failure categories, verdicts.

This is where raw engine output is turned into an explainable verdict.
Failure *causes* and *uncertain conclusions* are always reported as separate
lists — they are never folded into a generic pass/fail boolean.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict

from .engine import TraceResult

CATEGORY_DESCRIPTIONS = {
    "DUPLICATE": "identical sequence number received again; first copy kept",
    "REORDERED": "packet accepted behind the highest seen seq (out of order)",
    "LATE_AFTER_PLAYOUT":
        "packet arrived after its slot was played or declared a gap; "
        "discarded and never reinserted",
    "BUFFER_FULL":
        "bounded buffer was full; packet rejected rather than growing memory",
    "GAP": "a packet was missing at its playout deadline; explicit gap "
           "marker emitted, no audio was synthesised",
    "PARSE_ERROR": "malformed RTP datagram rejected at parse time",
}


def mode_summary(result: TraceResult) -> Dict[str, Any]:
    delays = [p.delay_ms for p in result.playout if p.kind == "AUDIO"]
    gap_positions = [p.ext_seq for p in result.playout if p.kind == "GAP"]
    return {
        "mode": result.mode,
        "monotonic_playout": result.monotonic,
        "monotonic_violations": result.monotonic_violations,
        "buffer_bounded": result.buffer_bounded,
        "max_buffer_observed": result.max_buffer_observed,
        "playout_items": len(result.playout),
        "audio_items": result.totals.get("played_audio", 0),
        "gap_items": result.totals.get("gaps", 0),
        "gap_positions": gap_positions,
        "drop_categories": {
            "DUPLICATE": result.totals.get("duplicates", 0),
            "REORDERED": result.totals.get("reordered", 0),
            "LATE_AFTER_PLAYOUT": result.totals.get("late_after_playout", 0),
            "BUFFER_FULL": result.totals.get("buffer_full", 0),
        },
        "parse_errors": result.totals.get("parse_errors", 0),
        "delay_ms": {
            "min": min(delays) if delays else None,
            "max": max(delays) if delays else None,
            "mean": sum(delays) / len(delays) if delays else None,
            "bounds": {
                "min_floor": result.config["min_delay_ms"],
                "max_ceiling": result.config["max_delay_ms"],
                "formula": f"clip(K*J + margin, min, max); K="
                           f"{result.config['jitter_multiplier']}, margin="
                           f"{result.config['safety_margin_ms']}ms",
            },
        },
        "sessions": [asdict(s) for s in result.sessions],
        "uncertainty": list(result.uncertainty),
    }


def compare(adaptive: TraceResult, fixed: TraceResult) -> Dict[str, Any]:
    a, f = mode_summary(adaptive), mode_summary(fixed)
    am, fm = a["delay_ms"]["mean"], f["delay_ms"]["mean"]

    def within_bounds(s: Dict[str, Any]) -> bool:
        d = s["delay_ms"]
        lo, hi = d["bounds"]["min_floor"], d["bounds"]["max_ceiling"]
        if d["min"] is None:
            return True
        return d["min"] >= lo - 1e-9 and d["max"] <= hi + 1e-9

    return {
        "adaptive": a,
        "fixed_baseline": f,
        "comparison": {
            "mean_delay_delta_ms": (am - fm) if (am is not None and fm is not None) else None,
            "gaps_adaptive": a["gap_items"],
            "gaps_fixed": f["gap_items"],
            "late_adaptive": a["drop_categories"]["LATE_AFTER_PLAYOUT"],
            "late_fixed": f["drop_categories"]["LATE_AFTER_PLAYOUT"],
            "both_monotonic": a["monotonic_playout"] and f["monotonic_playout"],
            "both_buffers_bounded": a["buffer_bounded"] and f["buffer_bounded"],
            "adaptive_delay_within_bounds": within_bounds(a),
        },
        "failure_category_glossary": CATEGORY_DESCRIPTIONS,
    }
