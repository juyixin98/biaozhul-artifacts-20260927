"""Audit interface.

A single :class:`Auditor` is handed to the security kernel. Every event is
persisted via :class:`app.state.Store` and optionally mirrored to stderr.

Privacy guarantee
-----------------
Audit records and stderr lines contain **only share fingerprints** (see
:func:`app.core.envelope.fingerprint`). They never contain the secret, the
share ``ys``, or the HMAC key. Diagnostics carry the request/collection id and
the categorical verdict + key state needed to explain accept / reject /
undecidable, without leaking sensitive material.
"""
from __future__ import annotations

import dataclasses
import sys
from typing import Sequence

from .state import Store


@dataclasses.dataclass
class AuditEvent:
    request_id: str
    collection_id: str | None
    action: str
    verdict: str
    detail: str | None = None
    fingerprints: Sequence[str] = dataclasses.field(default_factory=list)


class Auditor:
    def __init__(self, store: Store, *, to_stderr: bool = True):
        self._store = store
        self._to_stderr = to_stderr

    def record(self, event: AuditEvent) -> None:
        fps = list(event.fingerprints)
        self._store.append_audit(
            request_id=event.request_id,
            collection_id=event.collection_id,
            action=event.action,
            verdict=event.verdict,
            detail=event.detail,
            fingerprints=fps,
        )
        if self._to_stderr:
            # Redacted by construction: fingerprints only, no secret/share bytes.
            line = (
                f"[audit] req={event.request_id} coll={event.collection_id} "
                f"action={event.action} verdict={event.verdict} "
                f"shares={fps}"
            )
            if event.detail:
                line += f" detail={event.detail}"
            print(line, file=sys.stderr, flush=True)

    def query(
        self,
        *,
        request_id: str | None = None,
        collection_id: str | None = None,
        limit: int = 100,
    ):
        return self._store.query_audit(
            request_id=request_id,
            collection_id=collection_id,
            limit=limit,
        )
