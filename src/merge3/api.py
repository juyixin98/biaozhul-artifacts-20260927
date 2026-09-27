"""HTTP API (FastAPI).

Endpoints
---------
``POST /v1/merges``
    Body ``{document_id?, base_text, local_text, remote_text, request_id?}``
    -> 200 with ``status: "auto"`` and ``merged_text``, or
       ``status: "conflict"`` and full three-way conflict blocks.
``POST /v1/merges/{merge_id}/resolve``
    Body ``{document_id?, resolutions: {c1: {"choice": ..., "text"?}}}``
    -> 200 with rebuilt ``merged_text``.
``GET  /v1/merges/{merge_id}``
    Stored record: status, ranges, conflicts and recorded resolutions.
``GET  /v1/documents/{document_id}/versions``
    Version listing (metadata only — content fetched via merge records).
``GET  /health`` — liveness.

Conflict blocks never leave the service without the exact three-way source
ranges and alternatives, and no endpoint ever invents a resolution.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .service import MergeResponse, MergeService, ServiceError
from .storage import VersionStore


class MergeRequest(BaseModel):
    base_text: str = Field(description="Common baseline text, exact bytes.")
    local_text: str
    remote_text: str
    document_id: str = "default"
    request_id: Optional[str] = None


class ResolutionSpec(BaseModel):
    choice: str
    text: Optional[str] = None


class ResolveRequest(BaseModel):
    resolutions: dict[str, ResolutionSpec]
    document_id: str = "default"


def create_app(store: Optional[VersionStore] = None,
               settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or get_settings()
    store = store or VersionStore(settings.database_path)
    service = MergeService(store, settings)
    app = FastAPI(
        title="three-way structure-preserving merge backend",
        version="1.0.0",
    )
    app.state.store = store
    app.state.service = service

    @app.exception_handler(ServiceError)
    async def _service_error_handler(request, exc: ServiceError) -> JSONResponse:
        status_codes = {
            "input_invalid": 400,
            "payload_too_large": 413,
            "not_found": 404,
            "resolution_invalid": 409,
        }
        return JSONResponse(
            status_code=status_codes.get(exc.category, 400),
            content={"error": exc.category, "message": str(exc)},
        )

    def get_service() -> MergeService:
        return service

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/merges")
    async def post_merge(payload: MergeRequest,
                         svc: MergeService = Depends(get_service)) -> dict[str, Any]:
        response: MergeResponse = svc.run_merge(
            payload.base_text, payload.local_text, payload.remote_text,
            document_id=payload.document_id, request_id=payload.request_id,
        )
        return {
            "merge_id": response.merge_id,
            "request_id": response.request_id,
            "status": response.status,
            "merged_text": response.merged_text,
            "conflicts": response.conflicts,
            "edit_summary": response.edit_summary,
            "diagnostics": response.diagnostics,
        }

    @app.post("/v1/merges/{merge_id}/resolve")
    async def post_resolve(merge_id: str, payload: ResolveRequest,
                           svc: MergeService = Depends(get_service)) -> dict[str, Any]:
        resolutions = {cid: spec.model_dump(exclude_none=True)
                       for cid, spec in payload.resolutions.items()}
        rebuilt = svc.resolve(merge_id, resolutions,
                              document_id=payload.document_id)
        return {
            "merge_id": rebuilt.merge_id,
            "request_id": rebuilt.request_id,
            "status": rebuilt.status,
            "merged_text": rebuilt.merged_text,
        }

    @app.get("/v1/merges/{merge_id}")
    async def get_merge(merge_id: str,
                        svc: MergeService = Depends(get_service)) -> dict[str, Any]:
        return svc.get_merge(merge_id)

    @app.get("/v1/documents/{document_id}/versions")
    async def list_versions(document_id: str,
                            svc: MergeService = Depends(get_service)) -> dict[str, Any]:
        return {
            "document_id": document_id,
            "versions": svc.store.list_versions(document_id),
        }

    return app


app = create_app()
