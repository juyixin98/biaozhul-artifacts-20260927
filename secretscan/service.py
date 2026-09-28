"""Orchestration: run the engine, compute lifecycle states, build reports.

The service is the only layer that touches the database. It groups engine
candidates by content fingerprint, applies baseline exemptions, compares
against the previous scan (new / open / moved), resolves missing candidates
(known_fixed vs uncertain_removal), writes all state transitions and audit
events in one transaction, and assembles a public report that contains masks
and fingerprints only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import audit, state
from .baseline import Baseline, BaselineEntry
from .config import RulePack, ScopePack
from .scanner import (EngineResult, STATUS_IGNORED, STATUS_OVERSIZE,
                      STATUS_SYMLINK, STATUS_UNREADABLE, scan_snapshot)
from .security import Fingerprinter, RedactingFilter


def _redactor(logger: logging.Logger) -> RedactingFilter | None:
    """Find the redaction filter attached by :func:`audit.configure_logging`."""
    for handler in logger.handlers:
        for flt in handler.filters:
            if isinstance(flt, RedactingFilter):
                return flt
    return None

STATE_NEW = "new"
STATE_OPEN = "open"
STATE_MOVED = "moved"
STATE_KNOWN_FIXED = "known_fixed"
STATE_UNCERTAIN_REMOVAL = "uncertain_removal"
STATE_BASELINE_EXEMPT = "baseline_exempt"

# Failure / uncertainty reason codes (single source of truth for the docs).
REASON_TOO_LARGE = "file_too_large"
REASON_UNREADABLE = "file_unreadable"
REASON_SYMLINK = "symlink_not_followed"
REASON_IGNORED = "path_ignored_by_scope"
REASON_PATH_GONE = "old_path_gone"
REASON_IGNORED_NOW = "old_path_now_ignored"
REASON_FILE_CHANGED = "old_file_content_changed"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class FindingView:
    """One finding in a report — mask/fingerprint only, never the raw value."""

    finding_id: int
    state: str
    rule_id: str
    confidence: str
    mask: str
    fingerprint: str
    occurrences: list[dict]
    baseline_note: str | None = None
    resolution: dict | None = None
    entropy: float | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()
                if v is not None or k in ("resolution", "baseline_note")}


@dataclass
class ScanReport:
    scan_id: int
    root: str
    request_id: str
    actor_id: str
    started_at: str
    finished_at: str
    versions: dict
    counts: dict
    findings: dict[str, list[FindingView]] = field(default_factory=dict)
    unscanned: list[dict] = field(default_factory=list)
    ignored: list[dict] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    uncertainties: list[dict] = field(default_factory=list)
    baseline_path: str | None = None
    disclaimer: str = (
        "Every entry is a structural/entropy candidate, not a confirmed leak. "
        "No credential was validated over a network.")

    def to_dict(self) -> dict:
        return {
            "scan_id": self.scan_id,
            "root": self.root,
            "request_id": self.request_id,
            "actor_id": self.actor_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "versions": self.versions,
            "counts": self.counts,
            "findings": {
                state_name: [v.to_dict() for v in views]
                for state_name, views in self.findings.items()},
            "unscanned": self.unscanned,
            "ignored": self.ignored,
            "failures": self.failures,
            "uncertainties": self.uncertainties,
            "baseline_path": self.baseline_path,
            "disclaimer": self.disclaimer,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2,
                          sort_keys=True)


@dataclass(frozen=True)
class _Group:
    """All occurrences of one (fingerprint, rule_id) in the current scan."""

    fingerprint: str
    rule_id: str
    mask: str
    confidence: str
    entropy: float
    candidates: tuple[Any, ...]
    exempt_entry: BaselineEntry | None


def _group_candidates(engine: EngineResult,
                      baseline: Baseline | None) -> dict[tuple[str, str], _Group]:
    grouped: dict[tuple[str, str], _Group] = {}
    for cand in engine.candidates:
        key = (cand.fingerprint, cand.rule_id)
        existing = grouped.get(key)
        entry = baseline.lookup(cand.fingerprint, cand.rule_id) if baseline else None
        if existing is None:
            grouped[key] = _Group(
                fingerprint=cand.fingerprint, rule_id=cand.rule_id,
                mask=cand.mask, confidence=cand.confidence,
                entropy=cand.entropy, candidates=(cand,), exempt_entry=entry)
        else:
            grouped[key] = _Group(
                fingerprint=existing.fingerprint, rule_id=existing.rule_id,
                mask=existing.mask, confidence=existing.confidence,
                entropy=existing.entropy,
                candidates=existing.candidates + (cand,),
                exempt_entry=existing.exempt_entry or entry)
    return grouped


def _occurrence_dict(cand: Any) -> dict:
    return {
        "relpath": cand.relpath,
        "line": cand.line,
        "column": cand.column,
        "end_line": cand.end_line,
        "end_column": cand.end_column,
        "evidence": cand.evidence_masked,
        "entropy": round(cand.entropy, 4),
        "content_media": cand.content_media,
        "file_sha256": cand.file_sha256,
        "file_size": cand.file_size,
    }


def _resolution_for_missing(
    prev: state.PreviousOccurrence,
    file_index: dict[str, sqlite3.Row],
    current_paths_by_hash: dict[str, list[str]],
) -> tuple[str, dict]:
    """Classify a candidate that disappeared relative to the previous scan."""
    row = file_index.get(prev.relpath)
    if row is None:
        return STATE_UNCERTAIN_REMOVAL, {
            "reason_code": REASON_PATH_GONE,
            "reason": ("previous location is absent from this snapshot "
                       "(deleted, renamed outside the repo, or unreadable); "
                       "relocation cannot be ruled out"),
            "previous": {"relpath": prev.relpath, "mask": prev.mask,
                         "line": prev.line, "column": prev.column},
        }
    if row["status"] == STATUS_IGNORED:
        return STATE_UNCERTAIN_REMOVAL, {
            "reason_code": REASON_IGNORED_NOW,
            "reason": "previous location is now excluded by scope ignore rules",
            "previous": {"relpath": prev.relpath, "mask": prev.mask,
                         "line": prev.line, "column": prev.column},
        }
    if row["status"] != "scanned" or row["sha256"] != prev.file_sha256:
        # Same path exists but its contents changed and the fingerprint is
        # gone — this is the strongest offline evidence of remediation.
        return STATE_KNOWN_FIXED, {
            "reason_code": REASON_FILE_CHANGED,
            "reason": ("file still exists with different contents and the "
                       "candidate is absent; treated as known-fixed. Not "
                       "verified live and never confirmed as a real secret"),
            "previous": {"relpath": prev.relpath, "mask": prev.mask,
                         "line": prev.line, "column": prev.column},
            "current_file_sha256": row["sha256"],
        }
    # Same file, same content hash, but no candidate matched: a rule/scope
    # change is the likely cause — flag uncertainty rather than "fixed".
    return STATE_UNCERTAIN_REMOVAL, {
        "reason_code": "content_unchanged_but_no_match",
        "reason": ("file content is unchanged but no candidate matched; "
                   "likely a rule/scope pack change"),
        "previous": {"relpath": prev.relpath, "mask": prev.mask,
                     "line": prev.line, "column": prev.column},
    }


class ScanService:
    """Application service binding engine, state, baseline and audit log."""

    def __init__(self, conn: sqlite3.Connection, rules: RulePack,
                 scope: ScopePack, fingerprinter: Fingerprinter,
                 logger: logging.Logger,
                 baseline: Baseline | None = None):
        self.conn = conn
        self.rules = rules
        self.scope = scope
        self.fingerprinter = fingerprinter
        self.logger = logger
        self.baseline = baseline

    def _ensure_root_allowed(self, root: Path,
                             allowed_roots: tuple[Path, ...]) -> None:
        root = root.resolve()
        for allowed in allowed_roots:
            allowed = allowed.resolve()
            if root == allowed or allowed in root.parents:
                return
        audit.audit_event(
            self.logger, action=audit.ACT_API_DENIED, target_type="root",
            target=str(root), outcome="denied",
            reason_code="root_not_allowed")
        raise PermissionError(f"scan root outside allowed roots: {root}")

    def run_scan(self, root: str | Path,
                 ctx: audit.RequestContext,
                 allowed_roots: tuple[Path, ...] = ()) -> ScanReport:
        """Execute one full scan with lifecycle classification."""
        root = Path(root)
        if allowed_roots:
            self._ensure_root_allowed(root, allowed_roots)
        started = utcnow()
        with ctx:
            with self.conn:  # single transaction for scan state + audit
                scan_id = state.insert_scan(
                    self.conn,
                    started_at=started.isoformat(), root=str(root.resolve()),
                    rule_version=self.rules.version,
                    rule_fingerprint=self.rules.fingerprint(),
                    scope_version=self.scope.version,
                    scope_fingerprint=self.scope.fingerprint(),
                    pepper_id=self.fingerprinter.pepper_id,
                    request_id=ctx.request_id, actor_id=ctx.actor_id,
                    baseline_path=str(self.baseline.path) if self.baseline
                    else None)
                state.insert_audit(
                    self.conn, scan_id=scan_id, ts=started.isoformat(),
                    actor_id=ctx.actor_id, request_id=ctx.request_id,
                    action=audit.ACT_SCAN_STARTED, target_type="root",
                    target=str(root.resolve()),
                    details={"rule_version": self.rules.version,
                             "scope_version": self.scope.version},
                    outcome="ok")
                audit.audit_event(
                    self.logger, action=audit.ACT_SCAN_STARTED,
                    target_type="root", target=str(root.resolve()),
                    scan_id=scan_id,
                    rule_version=self.rules.version,
                    scope_version=self.scope.version)

                engine: EngineResult = scan_snapshot(
                    root, self.rules, self.scope, self.fingerprinter)
                state.insert_inventory(
                    self.conn, scan_id, engine.inventory)

                # Belt-and-braces: register every raw value so even an
                # accidental log statement renders only masks. Registrations
                # are per-scan: reset first so a later scan isn't scrubbed by
                # stale values that happen to match ordinary words.
                redactor = _redactor(self.logger)
                if redactor is not None:
                    redactor.clear()
                    redactor.register(c.secret.expose()
                                      for c in engine.candidates)

                file_index = state.current_file_index(self.conn, scan_id)
                prev_id = state.latest_scan_id_excluding(self.conn, scan_id)
                previous = (state.previous_occurrences(self.conn, prev_id)
                            if prev_id is not None else [])
                prev_by_key: dict[tuple[str, str],
                                  list[state.PreviousOccurrence]] = {}
                for po in previous:
                    prev_by_key.setdefault(
                        (po.fingerprint, po.rule_id), []).append(po)

                groups = _group_candidates(engine, self.baseline)
                finished = utcnow()
                seen_keys: set[tuple[str, str]] = set()
                views: dict[str, list[FindingView]] = {
                    STATE_NEW: [], STATE_OPEN: [], STATE_MOVED: [],
                    STATE_KNOWN_FIXED: [], STATE_UNCERTAIN_REMOVAL: [],
                    STATE_BASELINE_EXEMPT: [],
                }
                for (fp, rule_id), group in groups.items():
                    seen_keys.add((fp, rule_id))
                    prior = prev_by_key.get((fp, rule_id), [])
                    prior_paths = {p.relpath for p in prior}
                    current_paths = {c.relpath for c in group.candidates}
                    if group.exempt_entry is not None:
                        lifecycle = STATE_BASELINE_EXEMPT
                        action = audit.ACT_FINDING_EXEMPT
                    elif not prior:
                        lifecycle = STATE_NEW
                        action = audit.ACT_FINDING_NEW
                    elif current_paths - prior_paths:
                        lifecycle = STATE_MOVED
                        action = audit.ACT_FINDING_MOVED
                    else:
                        lifecycle = STATE_OPEN
                        action = audit.ACT_FINDING_OPEN
                    finding_id = state.upsert_finding(
                        self.conn, fingerprint=fp, rule_id=rule_id,
                        mask=group.mask, scan_id=scan_id,
                        seen_at=finished.isoformat(), state=lifecycle,
                        confidence=group.confidence)
                    exempt = group.exempt_entry is not None
                    for cand in group.candidates:
                        state.insert_occurrence(
                            self.conn, scan_id=scan_id, finding_id=finding_id,
                            candidate=cand, exempt=exempt)
                    details = {
                        "rule_id": rule_id, "mask": group.mask,
                        "confidence": group.confidence,
                        "occurrence_count": len(group.candidates),
                        "paths": sorted(current_paths),
                    }
                    state.insert_audit(
                        self.conn, scan_id=scan_id, ts=finished.isoformat(),
                        actor_id=ctx.actor_id, request_id=ctx.request_id,
                        action=action, target_type="finding",
                        target=f"{rule_id}:{fp[:12]}", details=details,
                        outcome="ok")
                    audit.audit_event(
                        self.logger, action=action, target_type="finding",
                        target=f"{rule_id}:{fp[:12]}", scan_id=scan_id,
                        **details)
                    view = FindingView(
                        finding_id=finding_id, state=lifecycle,
                        rule_id=rule_id, confidence=group.confidence,
                        mask=group.mask, fingerprint=fp,
                        occurrences=[_occurrence_dict(c)
                                     for c in group.candidates],
                        baseline_note=(group.exempt_entry.note
                                       if group.exempt_entry else None),
                        entropy=round(group.entropy, 4))
                    views[lifecycle].append(view)

                # Candidates present previously but absent now.
                for key, prev_occurrences in prev_by_key.items():
                    if key in seen_keys:
                        continue
                    for prev in prev_occurrences:
                        new_state, resolution = _resolution_for_missing(
                            prev, file_index, current_paths_by_hash={})
                        state.mark_finding_state(
                            self.conn, prev.finding_id, new_state, scan_id,
                            finished.isoformat())
                        action = (audit.ACT_FINDING_KNOWN_FIXED
                                  if new_state == STATE_KNOWN_FIXED
                                  else audit.ACT_FINDING_UNCERTAIN)
                        details = {
                            "rule_id": prev.rule_id, "mask": prev.mask,
                            "previous_path": prev.relpath,
                            "reason_code": resolution["reason_code"],
                        }
                        state.insert_audit(
                            self.conn, scan_id=scan_id,
                            ts=finished.isoformat(), actor_id=ctx.actor_id,
                            request_id=ctx.request_id, action=action,
                            target_type="finding",
                            target=f"{prev.rule_id}:{prev.fingerprint[:12]}",
                            details=details, outcome="ok")
                        audit.audit_event(
                            self.logger, action=action,
                            target_type="finding",
                            target=f"{prev.rule_id}:{prev.fingerprint[:12]}",
                            scan_id=scan_id, **details)
                        views[new_state].append(FindingView(
                            finding_id=prev.finding_id, state=new_state,
                            rule_id=prev.rule_id, confidence="unknown",
                            mask=prev.mask, fingerprint=prev.fingerprint,
                            occurrences=[], resolution=resolution))

                unscanned, ignored, failures = self._coverage_sections(engine)
                uncertainties = [v.to_dict()
                                 for v in views[STATE_UNCERTAIN_REMOVAL]]
                counts = {
                    "files_total": len(engine.inventory),
                    "files_scanned": len(engine.by_status("scanned")),
                    "paths_ignored": len(engine.by_status("ignored")),
                    "files_ignored": len([
                        f for f in engine.inventory
                        if f.status == STATUS_IGNORED
                        and not f.relpath.endswith("/")]),
                    "directories_pruned": len([
                        f for f in engine.inventory
                        if f.status == STATUS_IGNORED
                        and f.relpath.endswith("/")]),
                    "files_unscanned": len(engine.unscanned()),
                    "candidate_occurrences": len(engine.candidates),
                    "findings_new": len(views[STATE_NEW]),
                    "findings_open": len(views[STATE_OPEN]),
                    "findings_moved": len(views[STATE_MOVED]),
                    "findings_known_fixed": len(views[STATE_KNOWN_FIXED]),
                    "findings_uncertain_removal": len(uncertainties),
                    "findings_baseline_exempt": len(
                        views[STATE_BASELINE_EXEMPT]),
                }
                summary = {"counts": counts}
                state.complete_scan(self.conn, scan_id,
                                    finished.isoformat(), summary)
                state.insert_audit(
                    self.conn, scan_id=scan_id, ts=finished.isoformat(),
                    actor_id=ctx.actor_id, request_id=ctx.request_id,
                    action=audit.ACT_SCAN_COMPLETED, target_type="scan",
                    target=str(scan_id), details=summary, outcome="ok")
                audit.audit_event(
                    self.logger, action=audit.ACT_SCAN_COMPLETED,
                    target_type="scan", target=str(scan_id),
                    scan_id=scan_id, **summary)

                versions = {
                    "rule_pack": self.rules.fingerprint(),
                    "scope_pack": self.scope.fingerprint(),
                    "fingerprint_pepper_id": self.fingerprinter.pepper_id,
                    "baseline": (str(self.baseline.path)
                                 if self.baseline else None),
                }
                return ScanReport(
                    scan_id=scan_id, root=str(root.resolve()),
                    request_id=ctx.request_id, actor_id=ctx.actor_id,
                    started_at=started.isoformat(),
                    finished_at=finished.isoformat(), versions=versions,
                    counts=counts, findings=views, unscanned=unscanned,
                    ignored=ignored, failures=failures,
                    uncertainties=uncertainties,
                    baseline_path=str(self.baseline.path)
                    if self.baseline else None)

    def _coverage_sections(self, engine: EngineResult) -> tuple[
            list[dict], list[dict], list[dict]]:
        unscanned: list[dict] = []
        ignored: list[dict] = []
        failures: list[dict] = []
        for f in engine.inventory:
            row = {"relpath": f.relpath, "size": f.size,
                   "status": f.status, "reason": f.reason}
            if f.status == STATUS_IGNORED:
                ignored.append(row)
            elif f.status == STATUS_OVERSIZE:
                unscanned.append({**row, "reason_code": REASON_TOO_LARGE,
                                  "detail": "file not opened; explicitly "
                                            "NOT SCANNED per size limit"})
            elif f.status == STATUS_SYMLINK:
                unscanned.append({**row, "reason_code": REASON_SYMLINK})
            elif f.status == STATUS_UNREADABLE:
                failures.append({**row, "reason_code": REASON_UNREADABLE})
        return unscanned, ignored, failures


def load_report(conn: sqlite3.Connection, scan_id: int) -> dict | None:
    """Reconstruct a report for a stored scan (mask/fingerprint only)."""
    scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
    if scan is None:
        return None
    findings_rows = conn.execute(
        """
        SELECT f.* FROM findings f
        WHERE f.latest_scan_id=?
        ORDER BY f.id
        """, (scan_id,)).fetchall()
    grouped: dict[str, list[dict]] = {}
    for f in findings_rows:
        occs = conn.execute(
            "SELECT * FROM occurrences WHERE scan_id=? AND finding_id=? "
            "ORDER BY relpath, line, column",
            (scan_id, f["id"])).fetchall()
        grouped.setdefault(f["state"], []).append({
            "finding_id": f["id"], "state": f["state"],
            "rule_id": f["rule_id"], "confidence": f["confidence"],
            "mask": f["mask"], "fingerprint": f["fingerprint"],
            "occurrences": [{
                "relpath": o["relpath"], "line": o["line"],
                "column": o["column"], "end_line": o["end_line"],
                "end_column": o["end_column"], "evidence": o["evidence_masked"],
                "entropy": round(o["entropy"], 4),
                "content_media": o["content_media"],
                "file_sha256": o["file_sha256"], "file_size": o["file_size"],
                "exempt": bool(o["exempt"]),
            } for o in occs],
        })
    inventory = conn.execute(
        "SELECT * FROM file_inventory WHERE scan_id=? ORDER BY relpath",
        (scan_id,)).fetchall()
    unscanned, ignored, failures = [], [], []
    for inv in inventory:
        row = {"relpath": inv["relpath"], "size": inv["size"],
               "status": inv["status"], "reason": inv["reason"]}
        if inv["status"] == STATUS_IGNORED:
            ignored.append(row)
        elif inv["status"] == STATUS_OVERSIZE:
            unscanned.append({**row, "reason_code": REASON_TOO_LARGE})
        elif inv["status"] == STATUS_SYMLINK:
            unscanned.append({**row, "reason_code": REASON_SYMLINK})
        elif inv["status"] == STATUS_UNREADABLE:
            failures.append({**row, "reason_code": REASON_UNREADABLE})
    summary = json.loads(scan["summary_json"] or "{}")
    return {
        "scan_id": scan["id"], "root": scan["root"],
        "request_id": scan["request_id"], "actor_id": scan["actor_id"],
        "started_at": scan["started_at"], "finished_at": scan["finished_at"],
        "status": scan["status"],
        "versions": {
            "rule_pack": scan["rule_pack_fingerprint"],
            "scope_pack": scan["scope_pack_fingerprint"],
            "fingerprint_pepper_id": scan["pepper_id"],
            "baseline": scan["baseline_path"],
        },
        "counts": summary.get("counts", {}),
        "findings": grouped,
        "unscanned": unscanned, "ignored": ignored, "failures": failures,
        "uncertainties": grouped.get(STATE_UNCERTAIN_REMOVAL, []),
        "disclaimer": ("Every entry is a structural/entropy candidate, not a "
                       "confirmed leak. No credential was validated over a "
                       "network."),
    }
