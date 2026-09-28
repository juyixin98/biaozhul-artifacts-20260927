"""Tamper-evident audit trail.

Two complementary mechanisms:

* ``audit/events.jsonl`` — append-only JSONL event log where each record embeds
  ``prev_hash`` and its own ``record_hash`` (SHA-256 over canonical JSON),
  forming a verifiable hash chain.  Any edited/removed record breaks it.
* ``audit/key.bin`` — a local synthetic HMAC key.  Each run produces a signed
  manifest ``manifests/<run_id>.json`` whose ``signature`` is an HMAC-SHA256
  (implemented with :mod:`cryptography`) over the canonical manifest body.

Log lines are also emitted through the standard ``logging`` module so test
runs capture version, progress, decision basis and correlation ids.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hmac as crypto_hmac
from cryptography.hazmat.primitives import hashes

from . import __version__ as SERVICE_VERSION
from .isolation import atomic_write_json

LOGGER_NAME = "archguard.audit"


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON encoding used for hashing and signing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def load_or_create_key(audit_dir: Path) -> bytes:
    key_path = audit_dir / "key.bin"
    if key_path.exists():
        key = key_path.read_bytes()
        if len(key) >= 32:
            return key
    key = os.urandom(32)
    # Exclusive create so two processes cannot silently mint different keys.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def hmac_sign(key: bytes, message: bytes) -> str:
    h = crypto_hmac.HMAC(key, hashes.SHA256())
    h.update(message)
    return h.finalize().hex()


def hmac_verify(key: bytes, message: bytes, signature_hex: str) -> bool:
    try:
        signature = bytes.fromhex(signature_hex)
    except ValueError:
        return False
    h = crypto_hmac.HMAC(key, hashes.SHA256())
    h.update(message)
    try:
        h.verify(signature)
        return True
    except Exception:  # noqa: BLE001 - InvalidSignature
        return False


class AuditLogger:
    """Thread-safe append-only chained event logger."""

    def __init__(self, home: Path) -> None:
        self._audit_dir = home / "audit"
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self._audit_dir / "events.jsonl"
        self._key = load_or_create_key(self._audit_dir)
        self._lock = threading.Lock()
        self._seq, self._prev_hash = self._load_tail()
        self._py_logger = logging.getLogger(LOGGER_NAME)
        self._sink = None

    def set_sink(self, sink) -> None:
        """Register ``sink(record)`` — used to mirror events into SQLite."""
        self._sink = sink

    @property
    def key(self) -> bytes:
        return self._key

    @property
    def log_path(self) -> Path:
        return self._log_path

    def _load_tail(self) -> tuple[int, str]:
        if not self._log_path.exists():
            return 0, ""
        seq = 0
        prev = ""
        with self._log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                seq = int(record["seq"])
                prev = record["record_hash"]
        return seq, prev

    def event(
        self,
        run_id: str,
        phase: str,
        name: str,
        *,
        detail: dict[str, Any] | None = None,
        level: int = logging.INFO,
    ) -> dict[str, Any]:
        record = {
            "service": "archguard",
            "version": SERVICE_VERSION,
            "run_id": run_id,
            "seq": 0,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            + f".{int((time.time() % 1) * 1000):03d}",
            "phase": phase,
            "event": name,
            "detail": detail or {},
        }
        with self._lock:
            self._seq += 1
            record["seq"] = self._seq
            record["prev_hash"] = self._prev_hash
            record["record_hash"] = self._hash_record(record)
            line = json.dumps(record, sort_keys=True, ensure_ascii=False)
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._prev_hash = record["record_hash"]
        if self._sink is not None:
            try:
                self._sink(record)
            except Exception:  # noqa: BLE001 - sink failure must not break audit
                self._py_logger.exception("audit sink failed")
        self._py_logger.log(
            level,
            "[%s] %s/%s %s",
            run_id[:8],
            phase,
            name,
            json.dumps(detail or {}, sort_keys=True, ensure_ascii=False),
        )
        return record

    @staticmethod
    def _hash_record(record: dict[str, Any]) -> str:
        import hashlib

        body = {k: v for k, v in record.items() if k != "record_hash"}
        return hashlib.sha256(canonical_json(body)).hexdigest()

    def write_manifest(
        self,
        run_id: str,
        *,
        status: str,
        input_name: str,
        input_sha256: str,
        input_size: int,
        fmt: str | None,
        verdict: dict[str, Any],
        plan_summary: dict[str, Any] | None,
        files: list[dict[str, Any]] | None,
        failure: dict[str, str | None] | None,
        budgets: dict[str, Any],
    ) -> Path:
        body = {
            "service": "archguard",
            "version": SERVICE_VERSION,
            "run_id": run_id,
            "status": status,
            "input": {
                "name": input_name,
                "size": input_size,
                "sha256": input_sha256,
            },
            "format": fmt,
            "verdict": verdict,
            "budgets": budgets,
            "plan": plan_summary,
            "files": files or [],
            "failure": failure,
            "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        body_bytes = canonical_json(body)
        envelope = {
            "manifest": body,
            "alg": "HMAC-SHA256",
            "signature": hmac_sign(self._key, body_bytes),
        }
        manifests_dir = self._audit_dir.parent / "manifests"
        manifests_dir.mkdir(parents=True, exist_ok=True)
        path = manifests_dir / f"{run_id}.json"
        atomic_write_json(path, json.dumps(envelope, sort_keys=True, indent=2))
        return path


# --------------------------------------------------------------------------- #
# Verification helpers (used by tests and the audit API)
# --------------------------------------------------------------------------- #

def verify_chain(log_path: Path) -> dict[str, Any]:
    """Recompute the whole chain; return counts or the first break."""
    import hashlib

    if not log_path.exists():
        return {"ok": True, "records": 0}
    count = 0
    prev = ""
    with log_path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            stored_prev = record.get("prev_hash", "")
            stored_hash = record.get("record_hash", "")
            body = {k: v for k, v in record.items() if k != "record_hash"}
            actual_hash = hashlib.sha256(canonical_json(body)).hexdigest()
            if stored_prev != prev:
                return {
                    "ok": False,
                    "records": count,
                    "break_line": line_no,
                    "reason": "prev_hash mismatch",
                }
            if stored_hash != actual_hash:
                return {
                    "ok": False,
                    "records": count,
                    "break_line": line_no,
                    "reason": "record_hash mismatch",
                }
            count += 1
            prev = stored_hash
    return {"ok": True, "records": count}


def verify_manifest_file(path: Path, key: bytes) -> dict[str, Any]:
    envelope = json.loads(path.read_text(encoding="utf-8"))
    body = envelope["manifest"]
    ok = hmac_verify(key, canonical_json(body), envelope["signature"])
    return {"ok": ok, "run_id": body.get("run_id"), "status": body.get("status")}
