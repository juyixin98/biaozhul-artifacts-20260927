"""Structured diagnostics: every accept / reject / pending decision is recorded.

Each record carries:

* ``request_id`` -- supplied by the HTTP client or generated for offline replay;
* key state -- active tip, active height, candidate weight, parent, height;
* the outcome and, on rejection, the stable ``RejectReason`` code and detail.

The persisted record keeps full synthetic identifiers (needed to review a
specific block); stdout logs are redacted via :mod:`diag.masking`.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from ..storage.store import IndexStore, utc_now
from .masking import redact


def new_request_id() -> str:
    return "req-" + uuid.uuid4().hex[:16]


class Diagnostics:
    def __init__(self, store: IndexStore, *, log_level: str = "INFO"):
        self.store = store
        self.logger = logging.getLogger("reorgindex.diag")
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)
        self.logger.setLevel(log_level)
        self.logger.propagate = False

    def record(
        self,
        *,
        request_id: str,
        outcome: str,
        reason: Optional[str] = None,
        block_hash: Optional[str] = None,
        height: Optional[int] = None,
        parent: Optional[str] = None,
        weight: Optional[int] = None,
        detail: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> dict:
        tip = self.store.active_tip()
        record = {
            "request_id": request_id,
            "outcome": outcome,
            "reason": reason,
            "block_hash": block_hash,
            "height": height,
            "parent": parent,
            "active_tip": tip["hash"] if tip else None,
            "active_height": int(tip["height"]) if tip else None,
            "weight": weight,
            "detail": detail,
            "created_at": utc_now(),
        }
        self.store.insert_diagnostic(record)

        printable = dict(record)
        if extra:
            printable["extra"] = redact(extra)
        # stdout-safe form
        safe = redact(printable)
        line = json.dumps(safe, sort_keys=True, ensure_ascii=False)
        if reason:
            self.logger.warning(line)
        else:
            self.logger.info(line)
        return record
