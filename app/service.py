"""Orchestration: parse -> diagnose -> solve -> explain.

Every repair proposal keeps the original times and text and attaches a
human-readable reason per cue, so downstream reviewers can audit why each
displacement was suggested.
"""
import hashlib
from dataclasses import dataclass
from typing import Optional

from app.kernel.diagnostics import diagnose
from app.kernel.solver import solve_repair
from app.parsing import parse_document, render_document
from app.parsing.encoding import normalize_newlines


@dataclass
class ValidationOptions:
    min_duration_ms: int = 1000
    min_gap_ms: int = 0
    media_duration_ms: Optional[int] = None
    budget_ms: Optional[int] = None
    resolution_ms: Optional[int] = None
    allow_approximate: bool = False
    max_grid: int = 5000


def run_validation(content: str, fmt: str, options: ValidationOptions) -> dict:
    text = normalize_newlines(content)
    doc = parse_document(text, fmt)  # may raise SubtitleParseError
    cues = doc.cues
    diags = diagnose(cues,
                     min_duration_ms=options.min_duration_ms,
                     media_duration_ms=options.media_duration_ms)
    result = {
        "format": fmt,
        "cue_count": len(cues),
        "input_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "diagnostics": [d.to_dict() for d in diags],
    }
    if not diags:
        result["repair"] = {
            "status": "ALREADY_VALID",
            "minimal_change_ms": 0,
            "budget_ms": options.budget_ms,
            "proposal": None,
            "repaired_content": None,
            "trace": None,
        }
        return result

    res = solve_repair(
        [(c.start_ms, c.end_ms) for c in cues],
        min_duration_ms=options.min_duration_ms,
        min_gap_ms=options.min_gap_ms,
        media_duration_ms=options.media_duration_ms,
        budget_ms=options.budget_ms,
        resolution_ms=options.resolution_ms,
        allow_approximate=options.allow_approximate,
        max_grid=options.max_grid,
    )
    repair = {
        "status": res.status,
        "minimal_change_ms": res.minimal_change_ms,
        "budget_ms": options.budget_ms,
        "trace": res.trace,
        "proposal": None,
        "repaired_content": None,
    }
    if res.assignments is not None:
        by_cue = {}
        for d in diags:
            for ci in d.cue_indices:
                by_cue.setdefault(ci, []).append(d)
        proposal = []
        amap = {}
        for c, (ns, ne), change in zip(cues, res.assignments, res.per_cue_change_ms):
            amap[c.index] = (ns, ne)
            proposal.append({
                "index": c.index,
                "identifier": c.identifier,
                "original": {
                    "start_ms": c.start_ms,
                    "end_ms": c.end_ms,
                    "text": "\n".join(c.text_lines),
                },
                "proposed": {"start_ms": ns, "end_ms": ne},
                "change_ms": int(change),
                "reasons": _reasons(c, ns, ne, by_cue.get(c.index, []), options),
            })
        repair["proposal"] = proposal
        repair["repaired_content"] = render_document(doc, amap)
    result["repair"] = repair
    return result


def _reasons(cue, new_start, new_end, diags, options):
    reasons = []
    codes = {d.code for d in diags}
    moved = (new_start, new_end) != (cue.start_ms, cue.end_ms)
    if "NEGATIVE_DURATION" in codes or "ZERO_DURATION" in codes:
        reasons.append("non-positive display time rebuilt to satisfy the minimum duration")
    if "TOO_SHORT" in codes and new_end - new_start >= options.min_duration_ms:
        reasons.append(f"display time extended to the {options.min_duration_ms}ms minimum")
    if "OVERLAP" in codes and moved:
        reasons.append("shifted to remove overlap with an adjacent cue")
    if "MEDIA_BOUNDARY_EXCEEDED" in codes and moved:
        reasons.append("moved inside the media window")
    if not reasons:
        reasons.append("displaced to satisfy global timing constraints" if moved else "unchanged")
    return reasons
