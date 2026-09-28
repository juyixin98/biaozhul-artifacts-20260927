"""Structured diagnostics with record/request identifiers and redaction.

Every diagnostic explains *why* something was accepted, rejected or left
undecidable and carries the key state at that point (pid, byte offset,
counter values, table version, ...). Events are safe to print/log: raw
payloads never appear, byte strings are summarised by length, and long
strings are truncated.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

_MAX_STR = 200

# Event codes grouped by subsystem, kept in one place so tests and docs
# can refer to stable identifiers.
class Code(str, enum.Enum):
    # framing
    SYNC_LOCKED = "sync_locked"
    SYNC_LOST = "sync_lost"
    SYNC_RECOVERED = "sync_recovered"
    SYNC_RECOVERY_FAILED = "sync_recovery_failed"
    PACKET_PARSE_ERROR = "packet_parse_error"
    TRUNCATED_TAIL = "truncated_tail"
    # packet-level signals
    TRANSPORT_ERROR_INDICATOR = "transport_error_indicator"
    SCRAMBLED_PACKET = "scrambled_packet"
    # continuity
    CC_DUPLICATE = "duplicate_packet"
    CC_LOST = "continuity_lost"
    CC_STALL = "cc_stall_without_duplicate"
    CC_AF_ONLY_INCREMENT = "af_only_cc_increment"
    CC_DISCONTINUITY_FLAG = "discontinuity_indicator"
    CC_RESET_AFTER_SYNC = "cc_reset_after_sync_loss"
    # PSI / tables
    SECTION_INCOMPLETE = "section_incomplete"
    SECTION_OVERSIZED = "section_oversized"
    POINTER_OUT_OF_RANGE = "pointer_field_out_of_range"
    SECTION_GAP = "section_gap_after_cc_loss"
    TABLE_CRC_ERROR = "table_crc_error"
    TABLE_NOT_CURRENT = "table_not_current"
    TABLE_UNSUPPORTED = "unsupported_table"
    PAT_VERSION_SWITCH = "pat_version_switch"
    PAT_REPEAT = "pat_version_repeat"
    PMT_VERSION_SWITCH = "pmt_version_switch"
    PMT_REPEAT = "pmt_version_repeat"
    # PES
    PES_MIDSTREAM_DATA = "pes_midstream_data_without_start"
    PES_BAD_START_CODE = "pes_bad_start_code"
    PES_INCOMPLETE = "pes_incomplete"
    PES_OVERFLOW_CAP = "pes_payload_capped"
    PES_GAP = "pes_gap"
    # timing
    PCR_BACKWARDS = "pcr_backwards"


class Severity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


def redact(value: Any) -> Any:
    """Recursively turn an arbitrary context value into log-safe data."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<bytes:{len(value)}>"
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR else value[:_MAX_STR] + "...<truncated>"
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(v) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


@dataclass(frozen=True)
class DiagnosticEvent:
    record_id: str
    seq: int
    code: str
    severity: str
    message: str
    pid: Optional[int] = None
    offset: Optional[int] = None
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "seq": self.seq,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "pid": self.pid,
            "offset": self.offset,
            "context": self.context,
        }


EventListener = Callable[[DiagnosticEvent], None]


class DiagnosticsCollector:
    """Collects diagnostics for one analysis record (job/request id)."""

    def __init__(self, record_id: str, on_event: Optional[EventListener] = None):
        self.record_id = record_id
        self._events: list[DiagnosticEvent] = []
        self._on_event = on_event

    @property
    def events(self) -> list[DiagnosticEvent]:
        return self._events

    def emit(
        self,
        code: Code,
        severity: Severity,
        message: str,
        *,
        pid: Optional[int] = None,
        offset: Optional[int] = None,
        **context: Any,
    ) -> DiagnosticEvent:
        event = DiagnosticEvent(
            record_id=self.record_id,
            seq=len(self._events),
            code=code.value if isinstance(code, Code) else str(code),
            severity=severity.value if isinstance(severity, Severity) else str(severity),
            message=message,
            pid=pid,
            offset=offset,
            context=redact(context),
        )
        self._events.append(event)
        if self._on_event is not None:
            self._on_event(event)
        return event

    def info(self, code: Code, message: str, **kw: Any) -> DiagnosticEvent:
        return self.emit(code, Severity.INFO, message, **kw)

    def warning(self, code: Code, message: str, **kw: Any) -> DiagnosticEvent:
        return self.emit(code, Severity.WARNING, message, **kw)

    def error(self, code: Code, message: str, **kw: Any) -> DiagnosticEvent:
        return self.emit(code, Severity.ERROR, message, **kw)

    def codes(self) -> list[str]:
        return [e.code for e in self._events]

    def counts_by_severity(self) -> dict[str, int]:
        out = {"info": 0, "warning": 0, "error": 0}
        for event in self._events:
            out[event.severity] += 1
        return out

    def counts_by_code(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for event in self._events:
            out[event.code] = out.get(event.code, 0) + 1
        return out
