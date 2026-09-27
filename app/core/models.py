"""Domain model: cues, parse results and diagnostics."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# Supported document formats.
FORMAT_SRT = "srt"
FORMAT_VTT = "vtt"


class Severity(str, Enum):
    ERROR = "error"        # structural / timing problem that repair cannot cover
    WARNING = "warning"    # problem repair can address
    INFO = "info"          # note, e.g. odd-but-legal markup


@dataclass(frozen=True)
class Cue:
    """One subtitle cue.

    ``index`` is the zero-based order of appearance in the source document.
    ``raw_lines`` preserves the original payload lines verbatim (encoding and
    inline markup preserved); ``identifier`` preserves an optional VTT cue id.
    """

    index: int
    start_ms: int
    end_ms: int
    raw_lines: tuple[str, ...]
    identifier: str | None = None

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def text(self) -> str:
        return "\n".join(self.raw_lines)


@dataclass(frozen=True)
class Diagnostic:
    code: str                       # e.g. "overlap", "negative_duration"
    severity: Severity
    message: str
    cue_index: int | None = None    # first cue involved
    other_index: int | None = None  # second cue involved (overlaps)
    detail: dict = field(default_factory=dict)


@dataclass
class ParseResult:
    fmt: str
    cues: list[Cue] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    # Document-level prefix that must be preserved verbatim on re-serialization
    # (SRT: BOM if present; VTT: mandatory "WEBVTT" header).
    header: str = ""
    had_bom: bool = False

    @property
    def ok(self) -> bool:
        return not any(d.severity == Severity.ERROR for d in self.diagnostics)


@dataclass(frozen=True)
class CueRepair:
    """Per-cue repair decision. Original cue is never mutated or dropped."""

    cue_index: int
    original_start_ms: int
    original_end_ms: int
    repaired_start_ms: int
    repaired_end_ms: int
    shift_ms: int
    reasons: tuple[str, ...]       # diagnostic codes this cue was moved for
    action: str                    # "moved" | "held" | "flipped" | "extended"


@dataclass(frozen=True)
class RepairPlan:
    status: str                    # "repaired" | "infeasible_bounds" | "budget_exceeded"
    cues: tuple[CueRepair, ...]
    total_shift_ms: int
    max_shift_ms: int
    budget_ms: int
    message: str
