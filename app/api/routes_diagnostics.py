"""Diagnostic read endpoints (structured, redaction-safe records)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from .deps import get_container
from .schemas import DiagnosticEventOut

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


@router.get("/requests/{request_id}",
            response_model=list[DiagnosticEventOut])
def events_for_request(request_id: str, container=Depends(get_container)):
    rows = container.diag_repo.get_for_request(request_id)
    return [DiagnosticEventOut(**dict(r)) for r in rows]


@router.get("/events", response_model=list[DiagnosticEventOut])
def recent_events(
    container=Depends(get_container),
    limit: int = Query(default=100, ge=1, le=1000),
):
    rows = container.diag_repo.list_recent(limit)
    return [DiagnosticEventOut(**dict(r)) for r in rows]
