"""HTTP routes for the sparse Merkle state service."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse

from ..observability.diagnostics import event, new_request_id, redact_key, redact_value
from ..services.state_service import ServiceError
from .schemas import (
    BatchUpdateRequest,
    BatchUpdateResponse,
    EffectOut,
    ProofResponse,
    RootOut,
    RevisionListResponse,
    RevisionOut,
    VerifyRequest,
    VerifyResponse,
)

router = APIRouter(prefix="/api/v1")


def _request_id(request: Request, x_request_id: Optional[str]) -> str:
    rid = x_request_id or request.headers.get("x-request-id") or new_request_id()
    # Keep client supplied ids but bound them to a safe shape.
    return rid[:64]


def _svc(request: Request):
    return request.app.state.deps.service


def _logger(request: Request) -> logging.Logger:
    return request.app.state.deps.logger


# ---------------------------------------------------------------------------
# Health / roots
# ---------------------------------------------------------------------------
@router.get("/health")
def health(request: Request):
    svc = _svc(request)
    return {
        "status": "ok",
        "spec": "smt-v1",
        "revision": svc.revision,
        "root": svc.root.hex(),
    }


@router.get("/root", response_model=RootOut)
def current_root(request: Request, x_request_id: Optional[str] = Header(default=None)):
    rid = _request_id(request, x_request_id)
    svc = _svc(request)
    return RootOut(request_id=rid, revision=svc.revision, root=svc.root.hex())


@router.get("/revisions", response_model=RevisionListResponse)
def list_revisions(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    x_request_id: Optional[str] = Header(default=None),
):
    rid = _request_id(request, x_request_id)
    svc = _svc(request)
    return RevisionListResponse(
        request_id=rid, revisions=[RevisionOut(**r) for r in svc.store.list_revisions(limit)]
    )


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------
@router.post("/updates", response_model=BatchUpdateResponse)
def batch_update(
    body: BatchUpdateRequest, request: Request, x_request_id: Optional[str] = Header(default=None)
):
    rid = _request_id(request, x_request_id)
    svc = _svc(request)
    logger = _logger(request)
    try:
        effects = svc.apply_batch([(item.key, item.value) for item in body.updates])
    except ServiceError as exc:
        event(
            logger, logging.WARNING, "batch rejected", rid,
            category=exc.category,
            keys=[redact_key(item.key) for item in body.updates],
            root=svc.root.hex()[:16], revision=svc.revision,
        )
        return JSONResponse(
            status_code=409 if exc.category == "duplicate_key" else 400,
            content={
                "request_id": rid,
                "error": {"category": exc.category, "message": str(exc)},
            },
        )
    except (ValueError, TypeError) as exc:
        event(
            logger, logging.WARNING, "batch rejected: malformed key/value", rid,
            category="malformed_input", detail=str(exc),
        )
        return JSONResponse(
            status_code=400,
            content={
                "request_id": rid,
                "error": {"category": "malformed_input", "message": str(exc)},
            },
        )

    event(
        logger, logging.INFO, "batch accepted", rid,
        effect_count=len(effects),
        keys=[redact_key(e.key.hex()) for e in effects],
        values=[redact_value(e.value.hex() if e.value is not None else None) for e in effects],
        root=svc.root.hex()[:16], revision=svc.revision,
    )
    return BatchUpdateResponse(
        request_id=rid,
        revision=svc.revision,
        root=svc.root.hex(),
        effects=[
            EffectOut(
                seq=e.journal_seq,
                kind=e.kind,
                key=e.key.hex(),
                value_byte_length=0 if e.value is None else len(e.value),
                prev_root=e.prev_root.hex(),
                new_root=e.new_root.hex(),
            )
            for e in effects
        ],
    )


# ---------------------------------------------------------------------------
# State reads / proofs
# ---------------------------------------------------------------------------
@router.get("/values/{key_hex}")
def get_value(
    key_hex: str,
    request: Request,
    root: Optional[str] = Query(default=None, description="historical root hex"),
    x_request_id: Optional[str] = Header(default=None),
):
    rid = _request_id(request, x_request_id)
    svc = _svc(request)
    logger = _logger(request)
    try:
        root_bytes = bytes.fromhex(root) if root else None
        exists, value = svc.get_value(key_hex, root_bytes)
    except ValueError as exc:
        event(logger, logging.WARNING, "get rejected", rid, category="malformed_input", detail=str(exc))
        return JSONResponse(
            status_code=400,
            content={"request_id": rid, "error": {"category": "malformed_input", "message": str(exc)}},
        )
    except ServiceError as exc:
        event(
            logger, logging.WARNING, "get undecidable: unknown root", rid,
            category=exc.category, key=redact_key(key_hex),
        )
        return JSONResponse(
            status_code=404,
            content={"request_id": rid, "error": {"category": exc.category, "message": exc.args[0]}},
        )
    event(
        logger, logging.INFO, "get served", rid, key=redact_key(key_hex),
        exists=exists, value=redact_value(value.hex() if value is not None else None),
        root=(root_bytes or svc.root).hex()[:16],
    )
    return {
        "request_id": rid,
        "key": key_hex,
        "exists": exists,
        # Value is returned; it is synthetic local data. Logs only carry length.
        "value": None if value is None else value.decode("utf-8", errors="replace"),
        "root": (root_bytes or svc.root).hex(),
    }


@router.get("/proofs/{key_hex}", response_model=ProofResponse)
def issue_proof(
    key_hex: str,
    request: Request,
    root: Optional[str] = Query(default=None),
    compress: bool = Query(default=True),
    x_request_id: Optional[str] = Header(default=None),
):
    rid = _request_id(request, x_request_id)
    svc = _svc(request)
    logger = _logger(request)
    try:
        root_bytes = bytes.fromhex(root) if root else None
        proof = svc.issue_proof(key_hex, root_bytes, compress=compress)
    except ValueError as exc:
        event(logger, logging.WARNING, "proof rejected", rid, category="malformed_input", detail=str(exc))
        return JSONResponse(
            status_code=400,
            content={"request_id": rid, "error": {"category": "malformed_input", "message": str(exc)}},
        )
    except ServiceError as exc:
        event(logger, logging.WARNING, "proof undecidable", rid, category=exc.category,
              key=redact_key(key_hex), root=(root or "")[:16])
        return JSONResponse(
            status_code=404,
            content={"request_id": rid, "error": {"category": exc.category, "message": exc.args[0]}},
        )
    event(
        logger, logging.INFO, "proof issued", rid,
        key=redact_key(key_hex), exists=proof["exists"],
        terminal_depth=proof["terminal_depth"], steps=len(proof["steps"]),
        root=proof["root"][:16],
    )
    return ProofResponse(
        request_id=rid, root=svc.root.hex() if root_bytes is None else root_bytes.hex(),
        revision=svc.revision if root_bytes is None else None, proof=proof,
    )


@router.post("/proofs/verify", response_model=VerifyResponse)
def verify_proof_route(body: VerifyRequest, request: Request, x_request_id: Optional[str] = Header(default=None)):
    rid = _request_id(request, x_request_id)
    svc = _svc(request)
    logger = _logger(request)
    result = svc.check_proof(body.proof)
    event(
        logger,
        logging.INFO if result.ok else logging.WARNING,
        "proof verification",
        rid,
        verdict=result.verdict.value,
        reason=result.reason,
        key=redact_key(body.proof.get("key") if isinstance(body.proof, dict) else None),
        root=(body.proof.get("root", "")[:16]) if isinstance(body.proof, dict) else None,
    )
    return VerifyResponse(request_id=rid, valid=result.ok, verdict=result.verdict.value, reason=result.reason)
