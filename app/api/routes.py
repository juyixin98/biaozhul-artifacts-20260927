"""HTTP routes — thin adapters over GuardService."""
from __future__ import annotations

import platform
import sys

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from .. import __version__
from ..kernel.errors import ArchiveError
from ..service import ServiceResult
from .schemas import (
    EventOut,
    ExtractResponse,
    FailureResponse,
    InspectResponse,
    PlanSummary,
    RunSummary,
)

router = APIRouter()


def _plan_summary(result: ServiceResult) -> PlanSummary:
    s = result.plan.stats
    return PlanSummary(
        container=result.plan.container,
        entries=s.entries,
        files=s.files,
        directories=s.directories,
        symlinks=s.symlinks,
        total_declared_bytes=s.total_declared_bytes,
        compressed_bytes=s.compressed_bytes,
        worst_compression_ratio=round(s.worst_compression_ratio, 2),
        max_depth_seen=s.max_depth_seen,
    )


def _failure(result: ServiceResult) -> HTTPException:
    exc: ArchiveError | Exception = result.error
    if isinstance(exc, ArchiveError):
        body = FailureResponse(
            run_id=result.run_id,
            verdict="rejected",
            stage=result.stage,
            category=exc.category,
            message=exc.message,
            evidence=exc.evidence,
        )
        return HTTPException(status_code=exc.http_status, detail=body.model_dump())
    # Unknown/unexpected state: explicit 500, never a success response.
    body = FailureResponse(
        run_id=result.run_id,
        verdict="error",
        stage=result.stage or "unexpected",
        category=type(exc).__name__,
        message=str(exc),
    )
    return HTTPException(status_code=500, detail=body.model_dump())


async def _read_upload(file: UploadFile, max_bytes: int) -> bytes:
    data = await file.read()
    if len(data) > max_bytes:
        # Keep the service-level check authoritative; this is an early guard.
        from ..kernel.errors import UploadTooLarge

        raise UploadTooLarge(
            f"upload {len(data)} bytes exceeds limit {max_bytes}",
            evidence=f"size={len(data)}",
        )
    return data


@router.get("/health")
async def health(request: Request) -> dict:
    return {
        "status": "ok",
        "service": "archiveguard",
        "version": __version__,
        "config_version": request.app.state.settings.version,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }


@router.post("/api/v1/archives/inspect", response_model=InspectResponse)
async def inspect_archive(request: Request, file: UploadFile = File(...)):
    data = await _read_upload(file, request.app.state.settings.max_upload_bytes)
    result = request.app.state.service.inspect(data, file.filename or "upload.bin")
    if result.verdict != "accepted":
        raise _failure(result)
    return InspectResponse(
        run_id=result.run_id,
        verdict="accepted",
        input_sha256=request.app.state.audit.get_run(result.run_id)["input_sha256"],
        filename=file.filename,
        plan=_plan_summary(result),
        actions=[f"{a.kind}:{'/'.join(a.physical or a.canonical)}" for a in result.plan.actions],
    )


@router.post("/api/v1/archives/extract", response_model=ExtractResponse)
async def extract_archive(request: Request, file: UploadFile = File(...)):
    data = await _read_upload(file, request.app.state.settings.max_upload_bytes)
    result = request.app.state.service.extract(data, file.filename or "upload.bin")
    if result.verdict != "extracted":
        raise _failure(result)
    return ExtractResponse(
        run_id=result.run_id,
        verdict="extracted",
        input_sha256=request.app.state.audit.get_run(result.run_id)["input_sha256"],
        filename=file.filename,
        output_dir=str(result.workspace.output_dir),
        plan=_plan_summary(result),
        manifest=result.manifest,
    )


@router.get("/api/v1/runs/{run_id}", response_model=RunSummary)
async def get_run(run_id: str, request: Request):
    row = request.app.state.audit.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "run_not_found", "run_id": run_id})
    return RunSummary(**row)


@router.get("/api/v1/runs")
async def list_runs(request: Request, limit: int = 50):
    return {"runs": request.app.state.audit.list_runs(limit=min(limit, 200))}


@router.get("/api/v1/runs/{run_id}/events", response_model=list[EventOut])
async def get_events(run_id: str, request: Request):
    if request.app.state.audit.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail={"error": "run_not_found", "run_id": run_id})
    return [EventOut(**e) for e in request.app.state.audit.get_events(run_id)]


@router.get("/api/v1/audit/verify")
async def verify_chain(request: Request, run_id: str | None = None):
    return request.app.state.audit.verify_chain(run_id)
