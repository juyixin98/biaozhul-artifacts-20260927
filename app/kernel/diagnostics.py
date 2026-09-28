"""Timing diagnostics over parsed cues. Pure functions, no I/O.

Each diagnostic category is reported separately so callers can distinguish
negative duration, overlap, too-short display and media-boundary crossings.
"""
from dataclasses import dataclass

ERROR = "error"
WARNING = "warning"


@dataclass
class Diagnostic:
    code: str
    severity: str
    cue_indices: list
    message: str
    details: dict

    def to_dict(self):
        return {
            "code": self.code,
            "severity": self.severity,
            "cue_indices": list(self.cue_indices),
            "message": self.message,
            "details": self.details,
        }


def diagnose(cues, *, min_duration_ms, media_duration_ms=None):
    diags = []
    for c in cues:
        dur = c.end_ms - c.start_ms
        if dur < 0:
            diags.append(Diagnostic(
                "NEGATIVE_DURATION", ERROR, [c.index],
                f"cue {c.index} ends {abs(dur)}ms before it starts",
                {"start_ms": c.start_ms, "end_ms": c.end_ms, "duration_ms": dur},
            ))
        elif dur == 0:
            diags.append(Diagnostic(
                "ZERO_DURATION", ERROR, [c.index],
                f"cue {c.index} has zero display time",
                {"start_ms": c.start_ms, "end_ms": c.end_ms},
            ))
        elif dur < min_duration_ms:
            diags.append(Diagnostic(
                "TOO_SHORT", WARNING, [c.index],
                f"cue {c.index} shows for {dur}ms (< {min_duration_ms}ms minimum)",
                {"duration_ms": dur, "required_ms": min_duration_ms},
            ))
        if media_duration_ms is not None and (
            c.start_ms >= media_duration_ms or c.end_ms > media_duration_ms
        ):
            diags.append(Diagnostic(
                "MEDIA_BOUNDARY_EXCEEDED", ERROR, [c.index],
                f"cue {c.index} [{c.start_ms},{c.end_ms}] crosses media window "
                f"[0,{media_duration_ms}]",
                {"start_ms": c.start_ms, "end_ms": c.end_ms,
                 "media_duration_ms": media_duration_ms},
            ))
    for a, b in zip(cues, cues[1:]):
        if b.start_ms < a.start_ms:
            diags.append(Diagnostic(
                "OUT_OF_ORDER", WARNING, [a.index, b.index],
                f"cue {b.index} starts before the earlier cue {a.index}",
                {"prev_start_ms": a.start_ms, "start_ms": b.start_ms},
            ))
        if b.start_ms < a.end_ms:
            overlap = a.end_ms - b.start_ms
            diags.append(Diagnostic(
                "OVERLAP", ERROR, [a.index, b.index],
                f"cue {b.index} starts {overlap}ms before cue {a.index} ends",
                {"overlap_ms": overlap, "same_start": b.start_ms == a.start_ms},
            ))
    return diags
