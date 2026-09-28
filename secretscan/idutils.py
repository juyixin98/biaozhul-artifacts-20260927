"""Identifiers and small time helpers."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_scan_id() -> str:
    return "scan_" + uuid.uuid4().hex


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:16]


def project_id_for(root: str) -> str:
    return "proj_" + hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]
