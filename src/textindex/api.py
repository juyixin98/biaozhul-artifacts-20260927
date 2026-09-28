"""FastAPI application: HTTP boundary over :mod:`textindex.service`.

Two content types are accepted on create/raw because invalid UTF-8 cannot
travel through JSON (the JSON parser would reject the raw bytes):

* ``POST /documents``          — JSON body with a ``text`` string
* ``POST /documents/raw``      — raw body, decoded strictly by the service

Every error is rendered as the same envelope the error module defines::

    {"error": {"category", "code", "message", "details"},
     "run_id", "request_id"}

so the four failure categories are distinguishable over HTTP without
parsing messages.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import unicode_version
from .config import Settings
from .diagnostics import RunLogger
from .errors import TextIndexError
from .service import TextIndexService
from .storage import Storage


class CreateRequest(BaseModel):
    text: str = Field(..., description="document text (JSON string)")
    doc_id: str | None = Field(None, min_length=1, max_length=128)
    normalization: str | None = Field(
        None, description="NFC (default) | NFD | NFKC | NFKD | NONE"
    )


class EditRequest(BaseModel):
    start: int = Field(..., ge=0)
    end: int = Field(..., ge=0)
    replacement: str = ""
    unit: str = "grapheme"
    base_digest: str | None = None


class ConvertRequest(BaseModel):
    position: int = Field(..., ge=0)
    from_unit: str
    to_unit: str
    strict: bool = True


def create_app(settings: Settings | None = None,
               *, run_logger: RunLogger | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(
        title="Unicode Text Index Service",
        version="1.0.0",
        description=(
            "Byte / codepoint / extended-grapheme-cluster position indexing "
            f"on a fixed Unicode data version ({unicode_version.UNICODE_VERSION})."
        ),
    )

    storage = Storage(settings.db_path)
    logger = run_logger or RunLogger(settings.log_path)
    service = TextIndexService(settings, storage=storage, logger=logger)
    app.state.service = service
    app.state.settings = settings

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception:
            raise
        response.headers["x-request-id"] = request_id
        response.headers["x-run-id"] = logger.run_id
        return response

    def _rid(request: Request) -> str:
        return request.state.request_id

    def _error_response(exc: TextIndexError, request_id: str) -> JSONResponse:
        body = {
            "error": exc.to_dict(),
            "run_id": logger.run_id,
            "request_id": request_id,
        }
        return JSONResponse(status_code=exc.http_status, content=body)

    # --- meta ---------------------------------------------------------------

    @app.get("/version")
    def version() -> dict[str, str]:
        return {
            "service": "textindex",
            "service_version": "1.0.0",
            "unicode_version": unicode_version.UNICODE_VERSION,
            "segmenter": f"grapheme {unicode_version.GRAPHEME_LIB_VERSION}",
            "index_blob_format": str(unicode_version.BLOB_FORMAT_VERSION),
            "data_identity": unicode_version.DATA_VERSION_IDENTITY,
            "run_id": logger.run_id,
        }

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "run_id": logger.run_id}

    # --- documents ----------------------------------------------------------

    @app.post("/documents", status_code=201)
    def create_document(payload: CreateRequest, request: Request):
        try:
            return service.create_document(
                payload.text, doc_id=payload.doc_id,
                normalization=payload.normalization,
                request_id=_rid(request),
            )
        except TextIndexError as exc:
            return _error_response(exc, _rid(request))

    @app.post("/documents/raw", status_code=201)
    async def create_raw(request: Request):
        body = await request.body()
        try:
            return service.create_document(
                body, request_id=_rid(request))
        except TextIndexError as exc:
            return _error_response(exc, _rid(request))

    @app.get("/documents")
    def list_documents():
        return {"documents": service.storage.list_documents(),
                "run_id": logger.run_id}

    @app.get("/documents/{doc_id}")
    def get_document(doc_id: str, request: Request):
        try:
            return service.get_document(doc_id, request_id=_rid(request))
        except TextIndexError as exc:
            return _error_response(exc, _rid(request))

    @app.delete("/documents/{doc_id}", status_code=204)
    def delete_document(doc_id: str, request: Request):
        try:
            service.delete_document(doc_id, request_id=_rid(request))
            return JSONResponse(status_code=204, content=None)
        except TextIndexError as exc:
            return _error_response(exc, _rid(request))

    @app.get("/documents/{doc_id}/versions")
    def get_versions(doc_id: str, request: Request):
        try:
            return {"doc_id": doc_id,
                    "versions": service.list_versions(
                        doc_id, request_id=_rid(request))}
        except TextIndexError as exc:
            return _error_response(exc, _rid(request))

    @app.get("/documents/{doc_id}/clusters")
    def get_clusters(doc_id: str, request: Request):
        try:
            return service.clusters(doc_id, request_id=_rid(request))
        except TextIndexError as exc:
            return _error_response(exc, _rid(request))

    @app.post("/documents/{doc_id}/validate")
    def validate_document(doc_id: str, request: Request):
        try:
            return service.validate_index(doc_id, request_id=_rid(request))
        except TextIndexError as exc:
            return _error_response(exc, _rid(request))

    # --- edit + convert ------------------------------------------------------

    @app.post("/documents/{doc_id}/edit")
    def edit_document(doc_id: str, payload: EditRequest, request: Request):
        try:
            return service.edit_document(
                doc_id, start=payload.start, end=payload.end,
                replacement=payload.replacement, unit=payload.unit,
                base_digest=payload.base_digest,
                request_id=_rid(request),
            )
        except TextIndexError as exc:
            return _error_response(exc, _rid(request))

    @app.post("/documents/{doc_id}/convert")
    def convert_position(doc_id: str, payload: ConvertRequest,
                         request: Request):
        try:
            return service.convert(
                doc_id, position=payload.position,
                from_unit=payload.from_unit, to_unit=payload.to_unit,
                strict=payload.strict, request_id=_rid(request),
            )
        except TextIndexError as exc:
            return _error_response(exc, _rid(request))

    return app


app = create_app()
