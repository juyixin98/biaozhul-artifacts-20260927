"""FastAPI application exposing the verification backend."""
from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException
from pydantic import ValidationError as PydanticValidationError

from .api_models import (
    StatusResponse, StepDetail, ValidateRequest, ValidateResponse,
)
from .config import get_settings
from .logging_utils import bind_request_id, configure_logging, get_logger, reset_request_id
from .service import ValidationService
from .storage.metadata import get_store

settings = get_settings()
configure_logging(settings.log_level, settings.log_json)
log = get_logger("api")

app = FastAPI(
    title="Parquet restricted-nesting read/write validator",
    version="1.0.0",
    description="Self-implemented def/rep level kernel cross-checked against "
                "PyArrow for nested lists and nullable structs.",
)
service = ValidationService(get_store(settings))


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "parquet-nested-validator",
            "version": "1.0.0"}


@app.post("/api/v1/validate", response_model=ValidateResponse)
def validate(req: ValidateRequest) -> ValidateResponse:
    request_id = req.request_id or f"req-{uuid.uuid4().hex[:16]}"
    token = bind_request_id(request_id)
    log.info("validation request", extra={"context": {
        "request_id": request_id, "records": len(req.records)}})
    try:
        outcome = service.validate(
            request_id, req.schema_, req.records,
            expected_tree=req.expected_tree,
            page_size_bytes=req.page_size_bytes,
            force_page_after_records=req.force_page_after_records)
    finally:
        reset_request_id(token)
    return ValidateResponse(
        request_id=request_id,
        status=outcome.status,
        record_count=len(req.records),
        page_count=outcome.page_count,
        steps=[StepDetail(**s) for s in outcome.step_dicts()],
        mismatches=outcome.mismatches,
        uncertainties=outcome.uncertainties,
        warnings=outcome.warnings,
        artifact=outcome.artifact,
        error_category=outcome.error_category,
        error_message=outcome.error_message,
    )


@app.get("/api/v1/requests/{request_id}", response_model=StatusResponse)
def get_request(request_id: str) -> StatusResponse:
    row = service.store.get_request(request_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown request {request_id}")
    return StatusResponse(
        request_id=row["request_id"],
        status=row["status"],
        created_at=row["created_at"],
        record_count=row["record_count"],
        error_category=row["error_category"],
        error_detail=row["error_detail"],
        steps=[StepDetail(name=s["name"], status=s["status"],
                          detail=_safe_json(s.get("detail")))
               for s in row["steps"]],
        warnings=row["warnings"],
        artifact=row["artifact"],
    )


@app.get("/api/v1/requests")
def list_requests(limit: int = 50) -> dict:
    return {"requests": service.store.list_requests(limit)}


def _safe_json(value):
    import json
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {"text": value}
    return value
