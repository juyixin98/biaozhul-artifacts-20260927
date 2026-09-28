"""Audit trail: hash-chained, signed, request-correlated events.

Every event stores:

* run_id        -- which analysis run it belongs to (state isolation)
* request_id    -- which concrete request/witness it relates to, when applicable
* stage         -- machine-readable step (parse-refused, space-built, ...)
* caller_location -- source location that emitted it (explainability)
* detail        -- structured facts of the step
* prev_hash / entry_hash -- tamper-evident chain over canonical event bytes
* signature     -- Ed25519 signature over the same canonical bytes

``verify_chain`` recomputes the chain and every signature; a single modified
row breaks it and is reported as a BAD_SIGNATURE finding.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any, Callable

from .evidence import canonical_bytes, sha256_hex
from .signing import Ed25519PrivateKey, sign_object, verify_object
from .store import Store
from .types import Failure


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _caller_location(depth: int = 2) -> str:
    frame = inspect.currentframe()
    for _ in range(depth + 1):
        if frame is None:
            break
        frame = frame.f_back
    if frame is None:
        return "unknown"
    return f"{frame.f_code.co_filename.split('/')[-1]}:{frame.f_lineno}"


def chain_entry_bytes(event: dict[str, Any]) -> bytes:
    """Canonical bytes that define an audit entry's hash (excludes the hash and
    signature themselves)."""
    core = {k: event[k] for k in (
        "run_id", "request_id", "stage", "caller_location", "detail", "created_at", "prev_hash"
    ) if k in event}
    return canonical_bytes(core)


class AuditError(Exception):
    def __init__(self, code: Failure, message: str, *, findings: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.code = code
        self.findings = findings or []


class Auditor:
    def __init__(self, store: Store, signing_key: Ed25519PrivateKey | None = None):
        self.store = store
        self.key = signing_key

    def emit(self, stage: str, *, run_id: str | None = None, request_id: str | None = None,
             detail: dict[str, Any] | None = None, caller_location: str | None = None) -> dict[str, Any]:
        event = {
            "run_id": run_id,
            "request_id": request_id,
            "stage": stage,
            "caller_location": caller_location or _caller_location(),
            "detail": detail or {},
            "created_at": _now(),
        }
        event["prev_hash"] = self.store.latest_audit_hash()
        event["entry_hash"] = sha256_hex(chain_entry_bytes(event))
        event["signature"] = (
            sign_object(self.key, chain_entry_bytes(event)) if self.key is not None else None
        )
        self.store.append_audit_event(event)
        return event

    def as_diff_callback(self) -> Callable[..., None]:
        """Adapt to diff.run_diff(auditor=...) keyword callback style."""
        def _cb(*, run_id: str, stage: str, **fields: Any) -> None:
            request_id = fields.pop("request_id", None)
            self.emit(stage, run_id=run_id, request_id=request_id, detail=fields)
        return _cb

    def events(self, run_id: str | None = None, request_id: str | None = None) -> list[dict[str, Any]]:
        return self.store.audit_events(run_id=run_id, request_id=request_id)

    def verify_chain(self) -> dict[str, Any]:
        """Recompute hashes/signatures over the persisted chain."""
        findings: list[dict[str, Any]] = []
        prev = None
        rows = self.store.iter_audit_rows()
        pub = None
        try:
            pub = self.store.public_key()
        except Exception:
            findings.append({"problem": "no-registered-public-key"})
        count = 0
        for row in rows:
            count += 1
            event = {
                "run_id": row["run_id"],
                "request_id": row["request_id"],
                "stage": row["stage"],
                "caller_location": row["caller_location"],
                "detail": _safe_json(row["detail_json"]),
                "created_at": row["created_at"],
                "prev_hash": row["prev_hash"],
            }
            expected_prev = prev
            if row["prev_hash"] != expected_prev:
                findings.append({"seq": row["seq"], "problem": "chain-break",
                                 "expected_prev": expected_prev, "actual_prev": row["prev_hash"]})
            digest = sha256_hex(chain_entry_bytes(event))
            if digest != row["entry_hash"]:
                findings.append({"seq": row["seq"], "problem": "entry-hash-mismatch"})
            if pub is not None and row["signature"]:
                if not verify_object(pub, chain_entry_bytes(event), row["signature"]):
                    findings.append({"seq": row["seq"], "problem": "bad-signature"})
            elif row["signature"]:
                findings.append({"seq": row["seq"], "problem": "unsigned-key-unavailable"})
            prev = row["entry_hash"]
        if findings:
            raise AuditError(Failure.BAD_SIGNATURE, f"audit chain verification produced {len(findings)} finding(s)",
                             findings=findings)
        return {"ok": True, "events_verified": count}


def _safe_json(text: str) -> Any:
    import json
    return json.loads(text)
