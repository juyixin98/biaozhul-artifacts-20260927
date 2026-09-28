"""Result model.

These types are the security boundary of the scanner: once a raw matched
value has produced a ``masked`` representation and a ``fingerprint`` it is
discarded. No field anywhere here (and therefore nothing persisted or
reported) carries the original secret value.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

# Candidate lifecycle states (state machine, persisted by storage layer).
STATE_NEW = "new"
STATE_ACTIVE = "active"
STATE_REINTRODUCED = "reintroduced"
STATE_BASELINE_EXEMPT = "baseline_exempt"
STATE_KNOWN_FIXED = "known_fixed"

# Optional analyst triage.
TRIAGE_UNTRIPPED = "untriaged"
TRIAGE_CONFIRMED = "confirmed_leak"
TRIAGE_DISMISSED = "dismissed"

# Per-file outcome reasons.
REASON_TOO_LARGE = "too_large"
REASON_SYMLINK_SKIPPED = "symlink_skipped"
REASON_UNREADABLE = "unreadable"
REASON_UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class Location:
    path: str
    line: int          # 1-based for text; 1 for binary
    column: int        # 1-based start column for text; 1 for binary
    byte_offset: int   # 0-based offset within the file
    byte_end: int      # exclusive
    kind: str          # text | binary

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RawHit:
    """Internal carrier. Lives only inside one scan call; never persisted."""

    rule_id: str
    masked: str
    fingerprint: str
    entropy: float
    confidence: str
    category: str
    source: str            # rule | entropy
    uncertain: bool
    reasons: list[str] = field(default_factory=list)
    locations: list[Location] = field(default_factory=list)


@dataclass
class Candidate:
    rule_id: str
    category: str
    confidence: str
    masked: str
    fingerprint: str
    entropy: float
    source: str
    uncertain: bool
    reasons: list[str]
    first_seen_scan_id: str
    state: str
    triage: str
    locations: list[Location] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "confidence": self.confidence,
            "masked": self.masked,
            "fingerprint": self.fingerprint,
            "entropy": round(self.entropy, 4),
            "source": self.source,
            "uncertain": self.uncertain,
            "reasons": list(self.reasons),
            "first_seen_scan_id": self.first_seen_scan_id,
            "state": self.state,
            "triage": self.triage,
            "locations": [loc.to_dict() for loc in self.locations],
            "files": list(self.files),
        }


@dataclass
class FileError:
    path: str
    code: str
    message: str  # already redacted by the caller

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SkippedFile:
    path: str
    kind: str        # text | binary (when known)
    reason: str
    detail: str
    size_bytes: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScannedFile:
    path: str
    kind: str        # text | binary
    size_bytes: int
    sha256: str
    candidate_hits: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScanResult:
    scan_id: str
    project_id: str
    root: str
    rules_version: str
    classification_version: str
    config_digest: str
    started_at: str
    finished_at: str
    request_id: str
    candidates: list[Candidate] = field(default_factory=list)
    known_fixed: list[dict] = field(default_factory=list)
    files: list[ScannedFile] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    errors: list[FileError] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "files_scanned": len(self.files),
            "files_ignored": len(self.ignored),
            "files_skipped": len(self.skipped),
            "file_errors": len(self.errors),
            "candidates_total": len(self.candidates),
            "candidates_new": sum(c.state == STATE_NEW for c in self.candidates),
            "candidates_uncertain": sum(c.uncertain for c in self.candidates),
            "known_fixed": len(self.known_fixed),
        }

    def to_dict(self) -> dict:
        return {
            "scan_id": self.scan_id,
            "project_id": self.project_id,
            "root": self.root,
            "rules_version": self.rules_version,
            "classification_version": self.classification_version,
            "config_digest": self.config_digest,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "request_id": self.request_id,
            "summary": self.summary(),
            "candidates": [c.to_dict() for c in self.candidates],
            "known_fixed": list(self.known_fixed),
            "files": [f.to_dict() for f in self.files],
            "skipped": [s.to_dict() for s in self.skipped],
            "ignored": list(self.ignored),
            "errors": [e.to_dict() for e in self.errors],
        }
