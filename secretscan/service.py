"""Orchestration: scanner kernel + persistent state + audit trail.

State semantics (classification_version 1.0.0):
  new                - first time this (rule_id, content-fingerprint) is seen
  active             - seen again after first observation
  reintroduced       - was previously classified known_fixed, present again
  baseline_exempt    - operator accepted this exact content as a baseline
  known_fixed        - present in an earlier scan of this project, absent now.
                       "fixed" means "no longer observed"; the scanner is
                       offline and NEVER verifies credential revocation.

Content binding: baseline exemptions and state are keyed by
``(rule_id, fingerprint)`` where fingerprint is an HMAC of the matched
*content*. Moving a file changes the recorded location only; the candidate is
neither duplicated nor discharged.
"""

from __future__ import annotations

import functools
import json
import sqlite3
from pathlib import Path

from .errors import NotFoundError, StateConflictError, ValidationError
from .fingerprint import CandidateFingerprinter
from .idutils import new_request_id, new_scan_id, utcnow_iso
from .models import (
    STATE_ACTIVE,
    STATE_BASELINE_EXEMPT,
    STATE_KNOWN_FIXED,
    STATE_NEW,
    STATE_REINTRODUCED,
    TRIAGE_CONFIRMED,
    TRIAGE_DISMISSED,
    TRIAGE_UNTRIPPED,
)
from .rules import load_rules
from .scanner import Scanner
from .storage import Store

VALID_TRIAGE = {TRIAGE_UNTRIPPED, TRIAGE_CONFIRMED, TRIAGE_DISMISSED}


def serialized(fn):
    """Serialize a method on the store-wide RLock (multi-step DB transactions)."""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.store._lock:
            return fn(self, *args, **kwargs)

    return wrapper


class ScanService:
    def __init__(self, store: Store, config_path: str | Path):
        self.store = store
        self.config_path = Path(config_path)
        self._ruleset = load_rules(self.config_path)
        self._config_text = self.config_path.read_text(encoding="utf-8")

    # ---------------------------------------------------------------- helpers
    def _project_scanner(self, project_id: str) -> Scanner:
        master, salt = self.store.project_keys(project_id)
        return Scanner(self._ruleset, CandidateFingerprinter(master, salt))

    @staticmethod
    def _candidate_row_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["uncertain"] = bool(d["uncertain"])
        d["reasons"] = json.loads(d.pop("reasons_json"))
        return d

    # ------------------------------------------------------------------- run
    @serialized
    def run_scan(
        self,
        root: str | Path,
        *,
        actor: str,
        request_id: str | None = None,
        note: str = "",
    ) -> dict:
        request_id = request_id or new_request_id()
        root = str(Path(root).resolve())
        project_id, _, _, created = self.store.get_or_register_project(
            root, rules_version=self._ruleset.rules_version, note=note
        )
        scanner = self._project_scanner(project_id)
        scan_id = new_scan_id()
        result = scanner.scan(
            root,
            scan_id=scan_id,
            project_id=project_id,
            request_id=request_id,
        )
        result_dict = result.to_dict()

        conn = self.store.project_conn(project_id)
        current_fps: dict[tuple[str, str], dict] = {
            (c.rule_id, c.fingerprint): c.to_dict() for c in result.candidates
        }

        prev_rows = conn.execute(
            "SELECT rule_id, fingerprint, state FROM candidates"
        ).fetchall()
        prev = {(r["rule_id"], r["fingerprint"]): r["state"] for r in prev_rows}
        baseline_rows = conn.execute(
            "SELECT rule_id, fingerprint FROM baseline_exemptions"
        ).fetchall()
        baseline = {(r["rule_id"], r["fingerprint"]) for r in baseline_rows}

        # ---- classify candidates against history ------------------------
        persisted_candidates: list[dict] = []
        for cand in result.candidates:
            key = (cand.rule_id, cand.fingerprint)
            if key in baseline:
                state = STATE_BASELINE_EXEMPT
            elif key not in prev:
                state = STATE_NEW
            elif prev[key] == STATE_KNOWN_FIXED:
                state = STATE_REINTRODUCED
            else:
                state = STATE_ACTIVE
            cand.state = state
            persisted_candidates.append((key, cand))

        now = utcnow_iso()
        for key, cand in persisted_candidates:
            c = cand.to_dict()
            if key in prev:
                conn.execute(
                    "UPDATE candidates SET masked=?, category=?, confidence=?, entropy=?, "
                    "source=?, uncertain=?, reasons_json=?, state=?, state_updated_scan=?, "
                    "last_seen_scan_id=?, updated_at=? WHERE rule_id=? AND fingerprint=?",
                    (
                        c["masked"], c["category"], c["confidence"], c["entropy"],
                        c["source"], int(c["uncertain"]), json.dumps(c["reasons"]),
                        c["state"], scan_id, scan_id, now,
                        key[0], key[1],
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO candidates(rule_id, fingerprint, masked, category, "
                    "confidence, entropy, source, uncertain, reasons_json, state, triage, "
                    "first_seen_scan_id, last_seen_scan_id, state_updated_scan, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        key[0], key[1], c["masked"], c["category"], c["confidence"],
                        c["entropy"], c["source"], int(c["uncertain"]),
                        json.dumps(c["reasons"]), c["state"], TRIAGE_UNTRIPPED,
                        scan_id, scan_id, scan_id, now,
                    ),
                )

        # ---- known-fixed: previously observed, absent this scan ----------
        known_fixed: list[dict] = []
        for key, old_state in prev.items():
            if key in current_fps:
                continue
            row = conn.execute(
                "SELECT masked, category, confidence, source, uncertain, "
                "first_seen_scan_id, last_seen_scan_id, rule_id, fingerprint, triage "
                "FROM candidates WHERE rule_id=? AND fingerprint=?",
                key,
            ).fetchone()
            if row is None:
                continue
            is_baseline = key in baseline
            conn.execute(
                "UPDATE candidates SET state=?, state_updated_scan=?, updated_at=? "
                "WHERE rule_id=? AND fingerprint=?",
                (STATE_KNOWN_FIXED, scan_id, now, key[0], key[1]),
            )
            known_fixed.append({
                "rule_id": key[0],
                "fingerprint": key[1],
                "masked": row["masked"],
                "category": row["category"],
                "confidence": row["confidence"],
                "source": row["source"],
                "uncertain": bool(row["uncertain"]),
                "was_baseline_exempt": is_baseline,
                "previous_state": old_state,
                "last_seen_scan_id": row["last_seen_scan_id"],
                "fixed_in_scan_id": scan_id,
                "triage": row["triage"],
            })

        # ---- persist snapshot + audit ------------------------------------
        result_dict = result.to_dict()
        result_dict["known_fixed"] = known_fixed
        conn.execute(
            "INSERT INTO scans(scan_id, project_id, root, rules_version, "
            "classification_version, config_digest, request_id, started_at, finished_at, result_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                scan_id, project_id, root, self._ruleset.rules_version,
                self._ruleset.classification_version, self._ruleset.raw_digest,
                request_id, result_dict["started_at"], result_dict["finished_at"],
                json.dumps(result_dict, sort_keys=True),
            ),
        )
        conn.commit()
        self.store.append_project_audit(
            project_id,
            request_id=request_id,
            actor=actor,
            action="scan.run",
            detail={
                "scan_id": scan_id,
                "root": root,
                "rules_version": self._ruleset.rules_version,
                "config_digest": self._ruleset.raw_digest[:12],
                "files_scanned": result_dict["summary"]["files_scanned"],
                "files_skipped": result_dict["summary"]["files_skipped"],
                "file_errors": result_dict["summary"]["file_errors"],
                "candidates_total": result_dict["summary"]["candidates_total"],
                "known_fixed": len(known_fixed),
                "project_created": created,
            },
        )
        return result_dict

    # --------------------------------------------------------------- queries
    def _require_project(self, project_id: str) -> None:
        if self.store.get_project(project_id) is None:
            raise NotFoundError(f"unknown project_id {project_id}")

    def list_scans(self, project_id: str, limit: int = 50) -> list[dict]:
        self._require_project(project_id)
        conn = self.store.project_conn(project_id)
        rows = conn.execute(
            "SELECT scan_id, request_id, rules_version, config_digest, started_at, "
            "finished_at FROM scans ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_scan(self, project_id: str, scan_id: str) -> dict:
        self._require_project(project_id)
        conn = self.store.project_conn(project_id)
        row = conn.execute(
            "SELECT result_json FROM scans WHERE project_id=? AND scan_id=?",
            (project_id, scan_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown scan_id {scan_id}")
        return json.loads(row["result_json"])

    def latest_scan(self, project_id: str) -> dict:
        self._require_project(project_id)
        conn = self.store.project_conn(project_id)
        row = conn.execute(
            "SELECT scan_id FROM scans WHERE project_id=? ORDER BY started_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"no scans for project {project_id}")
        return self.get_scan(project_id, row["scan_id"])

    def list_candidates(self, project_id: str, *, state: str | None = None) -> list[dict]:
        self._require_project(project_id)
        conn = self.store.project_conn(project_id)
        if state:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE state=? ORDER BY updated_at DESC", (state,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM candidates ORDER BY state, updated_at DESC"
            ).fetchall()
        return [self._candidate_row_dict(r) for r in rows]

    def audit_trail(self, project_id: str, limit: int = 100) -> list[dict]:
        self._require_project(project_id)
        return self.store.list_project_audit(project_id, limit=limit)

    # -------------------------------------------------------------- baseline
    @serialized
    def accept_baseline(
        self,
        project_id: str,
        *,
        rule_id: str,
        fingerprint: str,
        actor: str,
        request_id: str | None = None,
        note: str = "",
    ) -> dict:
        request_id = request_id or new_request_id()
        self._require_project(project_id)
        conn = self.store.project_conn(project_id)
        row = conn.execute(
            "SELECT masked, state FROM candidates WHERE rule_id=? AND fingerprint=?",
            (rule_id, fingerprint),
        ).fetchone()
        if row is None:
            raise NotFoundError(
                "no candidate with that rule_id + content fingerprint; "
                "baseline binds to observed content, not to a file name"
            )
        if row["state"] == STATE_KNOWN_FIXED:
            raise StateConflictError(
                "candidate is currently known_fixed (absent); accept it from a scan "
                "where it is present"
            )
        conn.execute(
            "INSERT INTO baseline_exemptions(rule_id, fingerprint, masked, accepted_scan_id, "
            "request_id, actor, accepted_at, note) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(rule_id, fingerprint) DO UPDATE SET "
            "masked=excluded.masked, request_id=excluded.request_id, actor=excluded.actor, "
            "accepted_at=excluded.accepted_at, note=excluded.note",
            (
                rule_id, fingerprint, row["masked"], "", request_id, actor, utcnow_iso(), note,
            ),
        )
        conn.execute(
            "UPDATE candidates SET state=?, updated_at=? WHERE rule_id=? AND fingerprint=?",
            (STATE_BASELINE_EXEMPT, utcnow_iso(), rule_id, fingerprint),
        )
        conn.commit()
        self.store.append_project_audit(
            project_id,
            request_id=request_id,
            actor=actor,
            action="baseline.accept",
            detail={
                "rule_id": rule_id,
                "fingerprint": fingerprint,
                "masked": row["masked"],
                "note": note,
            },
        )
        return {"rule_id": rule_id, "fingerprint": fingerprint, "state": STATE_BASELINE_EXEMPT}

    @serialized
    def revoke_baseline(
        self,
        project_id: str,
        *,
        rule_id: str,
        fingerprint: str,
        actor: str,
        request_id: str | None = None,
    ) -> dict:
        request_id = request_id or new_request_id()
        self._require_project(project_id)
        conn = self.store.project_conn(project_id)
        cur = conn.execute(
            "DELETE FROM baseline_exemptions WHERE rule_id=? AND fingerprint=?",
            (rule_id, fingerprint),
        )
        if cur.rowcount == 0:
            raise NotFoundError("no baseline exemption for that rule_id + fingerprint")
        conn.commit()
        self.store.append_project_audit(
            project_id,
            request_id=request_id,
            actor=actor,
            action="baseline.revoke",
            detail={"rule_id": rule_id, "fingerprint": fingerprint},
        )
        return {"rule_id": rule_id, "fingerprint": fingerprint, "revoked": True}

    def list_baseline(self, project_id: str) -> list[dict]:
        self._require_project(project_id)
        conn = self.store.project_conn(project_id)
        rows = conn.execute(
            "SELECT rule_id, fingerprint, masked, actor, accepted_at, note, accepted_scan_id "
            "FROM baseline_exemptions ORDER BY accepted_at"
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- triage
    @serialized
    def triage_candidate(
        self,
        project_id: str,
        *,
        rule_id: str,
        fingerprint: str,
        triage: str,
        actor: str,
        request_id: str | None = None,
    ) -> dict:
        request_id = request_id or new_request_id()
        self._require_project(project_id)
        if triage not in VALID_TRIAGE:
            raise ValidationError(f"triage must be one of {sorted(VALID_TRIAGE)}")
        conn = self.store.project_conn(project_id)
        cur = conn.execute(
            "UPDATE candidates SET triage=?, updated_at=? WHERE rule_id=? AND fingerprint=?",
            (triage, utcnow_iso(), rule_id, fingerprint),
        )
        if cur.rowcount == 0:
            raise NotFoundError("no candidate with that rule_id + content fingerprint")
        conn.commit()
        self.store.append_project_audit(
            project_id,
            request_id=request_id,
            actor=actor,
            action="candidate.triage",
            detail={"rule_id": rule_id, "fingerprint": fingerprint, "triage": triage},
        )
        return {"rule_id": rule_id, "fingerprint": fingerprint, "triage": triage}
