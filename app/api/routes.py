"""HTTP routes and classified error mapping.

Nothing here swallows an exception into a success: kernel/service errors map
to an explicit ``error.code`` with a matching HTTP status, while verification
*results* (proofs that fail cryptographically) return 200 with
``valid=false`` and the concrete failure category -- rejecting a proof is the
normal successful output of the verify endpoint.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from app import SERVICE_NAME, SERVICE_VERSION
from app.audit.logging_config import bind_run, new_run_id
from app.config import PROTOCOL_VERSION
from app.core.errors import (
    CoreError,
    FailCategory,
)
from app.api.schemas import CreateBatchRequest, DiscloseRequest, VerifyRequest

router = APIRouter()

_HTTP_STATUS = {
    FailCategory.PROOF_MALFORMED: 400,
    FailCategory.TYPE_ENCODING_ERROR: 422,
    FailCategory.COMMITMENT_MISMATCH: 400,
    FailCategory.IDENTITY_MISMATCH: 422,
    FailCategory.MERKLE_PATH_MISMATCH: 400,
    FailCategory.ROOT_MISMATCH: 400,
    FailCategory.FIELD_NOT_COMMITTED: 404,
    FailCategory.RECORD_NOT_FOUND: 404,
    FailCategory.BATCH_NOT_FOUND: 404,
    FailCategory.POLICY_VIOLATION: 403,
    FailCategory.INTERNAL_ERROR: 500,
}


def _run_id(x_run_id: str | None) -> str:
    if x_run_id and x_run_id.strip():
        rid = x_run_id.strip()
        if len(rid) > 80:
            raise CoreError("X-Run-Id too long (max 80)")
        return rid
    return new_run_id()


def _service(request: Request, x_run_id: str | None):
    run_id = _run_id(x_run_id)
    log = bind_run(request.app.state.logger, run_id)
    return request.app.state.service_factory(log), run_id


@router.get("/health")
def health() -> dict[str, str]:
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "status": "ok",
    }


@router.post("/api/v1/batches", status_code=201)
def create_batch(
    body: CreateBatchRequest,
    request: Request,
    x_run_id: str | None = Header(default=None),
) -> Any:
    service, run_id = _service(request, x_run_id)
    try:
        result = service.create_batch(
            batch_id=body.batch_id,
            fields_raw=[f.model_dump() for f in body.fields],
            records_raw=body.records,
        )
    except CoreError as exc:
        return _error_response(exc, run_id)
    result["run_id"] = run_id
    return result


@router.get("/api/v1/batches")
def list_batches(request: Request, x_run_id: str | None = Header(default=None)) -> Any:
    service, run_id = _service(request, x_run_id)
    return {"run_id": run_id, "batches": service.db.list_public_batches()}


@router.get("/api/v1/batches/{batch_id}")
def get_batch(
    batch_id: str, request: Request, x_run_id: str | None = Header(default=None)
) -> Any:
    service, run_id = _service(request, x_run_id)
    try:
        public = service.db.public_batch(batch_id)
    except CoreError as exc:
        return _error_response(exc, run_id)
    public["run_id"] = run_id
    return public


@router.post("/api/v1/disclose")
def disclose(body: DiscloseRequest, request: Request, x_run_id: str | None = Header(default=None)) -> Any:
    service, run_id = _service(request, x_run_id)
    try:
        proof = service.disclose(
            batch_id=body.batch_id,
            record_index=body.record_index,
            path=body.path,
        )
    except CoreError as exc:
        return _error_response(exc, run_id)
    return {"run_id": run_id, "proof": proof}


@router.post("/api/v1/verify")
def verify(body: VerifyRequest, request: Request, x_run_id: str | None = Header(default=None)) -> Any:
    service, run_id = _service(request, x_run_id)
    try:
        verdict = service.verify(
            proof=body.proof,
            trusted_root_hex=body.trusted_batch_root_hex,
            expected_path=body.expected_path,
            expected_record_index=body.expected_record_index,
        )
    except CoreError as exc:
        return _error_response(exc, run_id)
    verdict["run_id"] = run_id
    return verdict


@router.get("/api/v1/audit/events")
def audit_events(
    request: Request,
    run_id: str | None = None,
    batch_id: str | None = None,
    x_run_id: str | None = Header(default=None),
) -> Any:
    service, current_run = _service(request, x_run_id)
    return {
        "run_id": current_run,
        "events": service.db.audit_events(run_id=run_id, batch_id=batch_id),
    }


def _error_response(exc: CoreError, run_id: str) -> JSONResponse:
    status = _HTTP_STATUS.get(exc.category, 500)
    return JSONResponse(
        status_code=status,
        content={
            "run_id": run_id,
            "error": {"code": exc.category.value, "message": str(exc)},
        },
    )
