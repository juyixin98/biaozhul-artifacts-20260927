"""HTTP 接口层（FastAPI）：请求/响应模型、run_id 中间件与统一错误信封。"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import settings
from .errors import (
    ErrorCategory,
    IndexServiceError,
    UnsupportedSpaceError,
    ValidationError as DomainValidationError,
)
from .logging_setup import configure_logging, path_var, run_id_var
from .service import TextIndexService
from .storage import VersionStore

Space = Literal["byte", "codepoint", "cluster"]


# ── 请求模型 ─────────────────────────────────────────────────────────────────
class CreateRequest(BaseModel):
    doc_id: str = Field(min_length=1, max_length=200)
    content_base64: str


class EditRequest(BaseModel):
    expected_version: int = Field(ge=0)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    space: Space
    replacement_base64: str


# ── 应用工厂 ─────────────────────────────────────────────────────────────────
def create_app(store: VersionStore | None = None) -> FastAPI:
    configure_logging(settings.log_level)
    app = FastAPI(
        title="textindex",
        version="1.0.0",
        description="基于固定 Unicode 数据版本的字节/码点/扩展字素簇位置索引服务",
    )
    own_store = store is None
    if store is None:
        store = VersionStore(settings.db_path)
    svc = TextIndexService(store, settings)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if own_store:
            store.close()

    app.router.lifespan_context = lifespan
    app.state.store = store
    app.state.service = svc

    @app.middleware("http")
    async def bind_run_id(request: Request, call_next):
        rid = request.headers.get("X-Run-ID") or f"run-{uuid.uuid4().hex[:12]}"
        run_id_var.set(rid)
        path_var.set(request.url.path)
        response = await call_next(request)
        response.headers["X-Run-ID"] = rid
        return response

    # ── 统一错误信封：四类错误可区分 ──
    @app.exception_handler(IndexServiceError)
    async def domain_error_handler(request: Request, exc: IndexServiceError):
        body = exc.to_dict()
        body["run_id"] = run_id_var.get()
        return JSONResponse(status_code=exc.http_status, content=body)

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "category": ErrorCategory.INPUT.value,
                "code": "VALIDATION_ERROR",
                "message": "请求参数校验失败",
                "details": {"errors": exc.errors()},
                "run_id": run_id_var.get(),
            },
        )

    # ── 路由 ─────────────────────────────────────────────────────────────────
    @app.get("/", tags=["meta"])
    def root() -> dict[str, str]:
        return {"service": "textindex", "docs": "/docs"}

    register_routes(app, svc)

    return app


def _validate_space(value: str) -> str:
    if value not in ("byte", "codepoint", "cluster"):
        raise UnsupportedSpaceError(
            f"不支持的位置空间: {value!r}", details={"space": value}
        )
    return value


def register_routes(app: FastAPI, svc: TextIndexService) -> None:
    # 健康检查必须暴露固定 Unicode 版本契约
    @app.get("/health", tags=["meta"])
    def health() -> dict[str, Any]:
        import grapheme
        import unicodedata
        return {
            "status": "ok",
            "gcb_table_version_pinned": settings.gcb_table_version,
            "grapheme_library_version": grapheme.UNICODE_VERSION,
            "unidata_version_pinned": settings.unidata_version,
            "unidata_runtime_version": unicodedata.unidata_version,
            "limits": {
                "max_bytes": settings.max_bytes,
                "max_codepoints": settings.max_codepoints,
                "max_clusters": settings.max_clusters,
                "max_documents": settings.max_documents,
            },
        }

    @app.post("/documents", tags=["documents"], status_code=201)
    def create_document(req: CreateRequest):
        rid = run_id_var.get()
        return svc.create_document(req.doc_id, req.content_base64, run_id=rid)

    @app.post("/documents/{doc_id}/edits", tags=["documents"])
    def edit_document(doc_id: str, req: EditRequest):
        return svc.edit_document(
            doc_id,
            expected_version=req.expected_version,
            start=req.start,
            end=req.end,
            space=req.space,
            replacement_base64=req.replacement_base64,
            run_id=run_id_var.get(),
        )

    @app.get("/documents/{doc_id}", tags=["documents"])
    def get_document(doc_id: str, version: int | None = None):
        return svc.get_version(doc_id, version)

    @app.get("/documents/{doc_id}/convert", tags=["query"])
    def convert(
        doc_id: str,
        position: int,
        from_space: str,
        to_space: str,
        version: int | None = None,
    ):
        if position < 0:
            from .errors import PositionOutOfRangeError
            raise PositionOutOfRangeError(
                "位置不能为负", details={"position": position}
            )
        _validate_space(from_space)
        _validate_space(to_space)
        return svc.convert_position(doc_id, version, position, from_space, to_space)

    @app.get("/documents/{doc_id}/versions", tags=["documents"])
    def versions(doc_id: str):
        return svc.list_versions(doc_id)

    @app.get("/documents/{doc_id}/clusters", tags=["diagnostics"])
    def clusters(doc_id: str, version: int | None = None, limit: int | None = None):
        if limit is not None and limit < 0:
            raise DomainValidationError(
                "limit 不能为负", details={"limit": limit}
            )
        return svc.diagnose_clusters(doc_id, version, limit)

    @app.get("/diagnostics/operations", tags=["diagnostics"])
    def operations():
        return {"operations": svc.diagnose_versions()}

    @app.get("/diagnostics/runs/{run_id}", tags=["diagnostics"])
    def replay(run_id: str):
        return svc.replay_run(run_id)
