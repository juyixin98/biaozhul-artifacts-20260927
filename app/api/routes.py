"""HTTP routes."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Request

from ..diagnostics import REJECT, RequestDiagnostics
from ..logging_setup import get_logger, set_request_id, reset_request_id
from ..redaction import redact_text
from ..storage.models import PublishValidationError
from ..storage.registry import VersionNotFoundError, VersionRegistry
from .deps import get_diagnostics, get_pinned_version, get_registry
from .errors import ApiError
from .schemas import PublishRequest, SegmentRequest

router = APIRouter()
logger = get_logger("http")


def _serialize(result) -> dict[str, Any]:
    return {
        "request_id": result.request_id,
        "version": result.version,
        "text_length": len(result.text),
        "normalized_text": result.normalized_text,
        "tokens": [
            {
                "kind": tok.kind,
                "surface": tok.surface,
                "display": tok.display,
                "cost": tok.cost,
                "norm_start": tok.norm_start,
                "norm_end": tok.norm_end,
                "orig_start": tok.orig_start,
                "orig_end": tok.orig_end,
                "is_unknown": tok.is_unknown,
            }
            for tok in result.tokens
        ],
        "best": {"surfaces": list(result.best.surfaces), "cost": result.best.cost,
                 "token_count": result.best.token_count},
        "runner_up": (
            None if result.runner_up is None
            else {"surfaces": list(result.runner_up.surfaces),
                  "cost": result.runner_up.cost,
                  "token_count": result.runner_up.token_count}
        ),
        "gap": result.gap,
        "gap_rounded": result.gap_rounded,
        "gap_status": result.gap_status,
        "coverage": {
            "orig_covered": result.orig_covered,
            "reconstructed": result.reconstructed,
            "orig_ranges": [[tok.orig_start, tok.orig_end] for tok in result.tokens],
        },
        "unknown_tokens": result.unknown_tokens,
        "removed_chars": result.removed_chars,
    }


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    registry: VersionRegistry = request.app.state.registry
    snapshot = registry.current()
    return {
        "status": "ok" if snapshot is not None else "degraded",
        "current_version": snapshot.version if snapshot else None,
        "word_count": snapshot.word_count() if snapshot else 0,
    }


@router.post("/segment")
def segment(
    body: SegmentRequest,
    request: Request,
    registry: VersionRegistry = Depends(get_registry),
    diag: RequestDiagnostics = Depends(get_diagnostics),
    pinned: Optional[str] = Depends(get_pinned_version),
) -> dict[str, Any]:
    token = set_request_id(diag.request_id)
    try:
        logger.info("segment request accepted input=%s pinned=%s",
                    redact_text(body.text, reveal=request.app.state.settings.log_reveal_text),
                    pinned or "current")
        try:
            result = registry.segment(body.text, diag, version_ref=pinned)
        except VersionNotFoundError:
            # registry.segment already recorded a REJECT diagnostics event.
            logger.warning("segment rejected: unknown version ref=%s", pinned)
            raise ApiError(
                status_code=409, code="version_not_found",
                message=f"dictionary version {pinned!r} is not available",
                details={"requested": pinned}, request_id=diag.request_id,
            )

        payload = _serialize(result)
        payload["diagnostics"] = diag.public_view()
        logger.info(
            "segment completed tokens=%d unknown=%d gap=%s coverage=%s",
            result.best.token_count, result.unknown_tokens, result.gap_status,
            result.orig_covered,
        )
        return payload
    finally:
        reset_request_id(token)


@router.post("/admin/dictionaries", status_code=201)
def publish_dictionary(
    body: PublishRequest,
    registry: VersionRegistry = Depends(get_registry),
    diag: RequestDiagnostics = Depends(get_diagnostics),
) -> dict[str, Any]:
    token = set_request_id(diag.request_id)
    try:
        raw_entries = [e.model_dump(exclude_none=True) for e in body.entries]
        try:
            prepared = registry.prepare(raw_entries)
        except PublishValidationError as exc:
            diag.add("publish_validation", REJECT,
                     f"{len(exc.issues)} invalid entry/entries; whole batch rejected",
                     issue_codes=sorted({i['code'] for i in exc.issues}),
                     issue_count=len(exc.issues))
            logger.warning("publish rejected issue_count=%d", len(exc.issues))
            raise ApiError(
                status_code=400, code="invalid_dictionary",
                message="dictionary batch failed validation; no version was published",
                details={"issues": exc.issues}, request_id=diag.request_id,
            )

        snapshot = registry.publish_prepared(prepared, note=body.note)
        diag.add("publish", "accept", "new complete dictionary version published",
                 version=snapshot.version, word_count=snapshot.word_count(),
                 total_frequency=snapshot.total_frequency)
        logger.info("dictionary published version=%s words=%d",
                    snapshot.version, snapshot.word_count())
        return {
            "version": snapshot.version,
            "entry_count": snapshot.word_count(),
            "total_frequency": snapshot.total_frequency,
            "diagnostics": diag.public_view(),
        }
    finally:
        reset_request_id(token)


@router.get("/admin/dictionaries")
def list_dictionaries(registry: VersionRegistry = Depends(get_registry)) -> dict[str, Any]:
    versions = registry.list_versions()
    return {
        "versions": [
            {
                "version": v.version,
                "created_at": v.created_at,
                "is_current": v.is_current,
                "entry_count": v.entry_count,
                "total_frequency": v.total_frequency,
                "checksum": v.checksum,
                "note": v.note,
            }
            for v in versions
        ]
    }
