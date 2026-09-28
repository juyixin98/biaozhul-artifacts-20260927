"""HTTP verification API (FastAPI).

Endpoints:
    POST /api/v1/schema/admit     -- schema admission + D/R metadata preview
    POST /api/v1/verify           -- full verification run
    GET  /api/v1/runs/{id}        -- fetch a persisted run with its events
    GET  /api/v1/runs             -- recent runs
    GET  /healthz                 -- liveness

Every verify response echoes the request id (also accepted as the
``X-Request-ID`` request header); failures carry the failure category and a
page/slot/record location.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..core.errors import ErrorCode, StructuredError
from .models import (
    SchemaAdmitRequest,
    VerifyRequest,
    VerifyResponse,
)
from .service import VerificationService


def create_app(service: VerificationService | None = None) -> FastAPI:
    app = FastAPI(
        title="Parquet restricted-list / nullable-struct verifier",
        version="1.0.0",
    )
    svc = service or VerificationService()
    app.state.service = svc

    @app.exception_handler(StructuredError)
    async def structured_error_handler(_: Request, exc: StructuredError):
        return JSONResponse(
            status_code=400 if exc.code not in (
                ErrorCode.PAGE_TRUNCATES_RECORD,
                ErrorCode.ROUNDTRIP_MISMATCH,
                ErrorCode.REFERENCE_MISMATCH,
                ErrorCode.STRUCT_CHILD_PRESENCE_MISMATCH,
                ErrorCode.COLUMN_RECORD_BOUNDARY_MISMATCH,
                ErrorCode.PAGE_INVARIANT_VIOLATION,
            ) else 422,
            content={"status": "FAILED", "error": exc.to_dict()},
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/schema/admit")
    @app.post("/api/v1/schema/admit")
    async def admit_schema(body: SchemaAdmitRequest) -> dict[str, Any]:
        schema = svc.admit_schema(body.schema_)
        return {
            "status": "OK",
            "leaf_columns": schema.describe_for_headers(),
            "node_count": len(schema.all_nodes),
        }

    @app.post("/api/v1/verify", response_model=VerifyResponse)
    async def verify_endpoint(
        body: VerifyRequest,
        request: Request,
        x_request_id: str | None = Header(default=None),
        include: str = Query(default=""),
    ) -> dict[str, Any]:
        request_id = x_request_id or f"req-{uuid.uuid4().hex[:12]}"
        result = svc.run_verification(
            body.schema_,
            body.records,
            request_id=request_id,
            expected=body.expected,
            page_slot_target=body.page_slot_target,
            parquet_page_bytes=body.parquet_page_bytes,
            page_version=body.page_version,
        )
        include_values = "values" in include.split(",")
        include_levels = "levels" in include.split(",")
        return {
            "request_id": request_id,
            "status": result["status"],
            "steps": result["steps"],
            "findings": result["findings"],
            "kernel_pages": result["kernel_pages"],
            "oracle_page_count": result["oracle_page_count"],
            "decoded": result["decoded"] if include_values else None,
            "pyarrow_values": result["pyarrow_values"]
            if include_values else None,
            "kernel_levels": result["kernel_levels"] if include_levels else None,
        }

    @app.get("/api/v1/runs/{request_id}")
    async def get_run(request_id: str) -> dict[str, Any]:
        run = svc.get_run(request_id)
        if run is None:
            raise HTTPException(status_code=404,
                                detail=f"unknown request_id {request_id}")
        return run

    @app.get("/api/v1/runs")
    async def list_runs(limit: int = Query(default=50, ge=1, le=500)):
        return {"runs": svc.store.list_runs(limit)}

    return app


app = create_app()
