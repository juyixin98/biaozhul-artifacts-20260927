"""Diagnostic record model.

Every accept/reject/undetermined decision made by the parsing kernels is a
:class:`Finding`.  Findings are machine-typed (``code``) and always carry the
packet index / PID / key state that motivated the verdict, so a caller never
has to infer *why* something was accepted, rejected, or could not be decided.

Dispositions
------------
``ACCEPTED``      conformant-but-noteworthy (e.g. a valid duplicate packet).
``REJECTED``      malformed or violating; the offending bytes were discarded
                  from the relevant reassembly state.
``UNDETERMINED``  not enough evidence to decide (e.g. first packet of a PID,
                  or a gap whose beginning is outside the scanned range).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Disposition(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNDETERMINED = "undetermined"


# ---------------------------------------------------------------------------
# Finding codes.  Kept as plain constants (rather than an Enum) so tests and
# the builder tools can import a shared vocabulary without importing kernels.
# ---------------------------------------------------------------------------
RESYNC_OCCURRED = "resync_occurred"
TRAILING_BYTES = "trailing_bytes"
NO_SYNC = "no_sync"
TEI_SET = "transport_error_indicator"

CC_BASELINE = "cc_baseline"
CC_DUPLICATE = "duplicate_packet"
CC_GAP = "cc_gap"
CC_REPEAT_PAYLOAD = "cc_repeat_payload_without_duplicate_bit"
CC_ADAPTATION_CC_MISMATCH = "adaptation_only_cc_mismatch"
CC_DISCONTINUITY = "signaled_discontinuity"
CC_DISCONTINUITY_NO_GAP = "discontinuity_flag_without_gap"

SECTION_CRC_ERROR = "section_crc_error"
SECTION_MALFORMED = "section_malformed"
SECTION_INCOMPLETE = "section_incomplete_at_end"
TABLE_VERSION_SWITCH = "table_version_switch"

PES_GAP = "pes_gap"
PES_MALFORMED = "pes_malformed"
PES_OVERSIZE = "pes_oversize"
PES_INCOMPLETE = "pes_incomplete_at_end"
PES_SCRAMBLED = "pes_scrambled_skipped"

PCR_NON_MONOTONIC = "pcr_non_monotonic"
UNKNOWN_PID = "unknown_pid_payload"


@dataclass
class Finding:
    """One diagnostic.  ``details`` holds the motivating key state."""

    code: str
    severity: Severity
    disposition: Disposition
    message: str
    packet_index: int | None = None
    pid: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["disposition"] = self.disposition.value
        return d


def redact_label(value: str, keep: int = 1) -> str:
    """Mask a potentially sensitive free-text label.

    Keeps the first ``keep`` characters and replaces the rest with ``*``.
    Used for any externally supplied names that end up in logs/diagnostics.
    Binary analysis input itself is not user-readable text and is not logged.
    """
    if value is None:
        return value
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)
