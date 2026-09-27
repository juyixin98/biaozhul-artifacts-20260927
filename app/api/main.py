"""FastAPI 应用工厂与路由。

失败响应统一形如::

    {"ok": false, "request_id": "...", "error": {"category": "...", "message": "...", ...}}

所有日志/轨迹均以 X-Request-ID（缺省由服务生成）关联，
失败原因（category）与不确定结论（uncertainties）分区呈现。
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..config import get_settings
from ..diagnostics.logging_setup import configure_logging
from ..diagnostics.request_context import header_or_new, set_request_id
from ..diagnostics.trace import Trace, TraceStore
from ..query.engine import Engine
from ..query.spec import ErrorCategory, QueryError
from ..storage.version_store import (
    VersionConflictError,
    VersionNotFoundError,
    VersionStore,
)
from .schemas import CommitRequest, QueryRequest

_CATEGORY_STATUS = {
    ErrorCategory.PARSE_ERROR.value: 400,
    ErrorCategory.UNKNOWN_TERM.value: 404,
    ErrorCategory.VERSION_NOT_FOUND.value: 404,
    ErrorCategory.VERSION_CONFLICT.value: 409,
    ErrorCategory.UNIVERSE_NOT_VERSIONED.value: 409,
    ErrorCategory.INTERNAL.value: 500,
}


def _category_of(exc: Exception) -> str:
    category = getattr(exc, "category", ErrorCategory.INTERNAL)
    return category.value if isinstance(category, ErrorCategory) else str(category)


def create_app(store: Optional[VersionStore] = None) -> FastAPI:
    settings = get_settings()
    logger = configure_logging(settings.log_path)
    app = FastAPI(
        title="持久 Posting 列表布尔查询服务",
        version="1.0.0",
        description=(
            "在显式版本化文档全集上执行 AND / OR / NOT（含短路求值与跳跃块统计）。\n\n"
            "查询语法：`(cat OR dog) AND NOT fish`；NOT 相对指定版本的有限全集求补。"
        ),
    )
    app.state.settings = settings
    app.state.store = store or VersionStore(settings.db_path)
    app.state.traces = TraceStore(settings.trace_ring_size)
    app.state.log = logger

    @app.middleware("http")
    async def request_identity(request: Request, call_next):
        request_id = header_or_new(request.headers.get("x-request-id"))
        set_request_id(request_id)
        start = time.perf_counter()
        logger.info(
            "request start",
            extra={
                "event": "request_start",
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
            },
        )
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "unhandled error",
                extra={"event": "unhandled_error", "request_id": request_id},
            )
            raise
        elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request end",
            extra={
                "event": "request_end",
                "request_id": request_id,
                "status_code": response.status_code,
                "elapsed_ms": elapsed_ms,
            },
        )
        return response

    def _error_response(exc: Exception, request_id: str, trace: Optional[Trace] = None) -> JSONResponse:
        category = _category_of(exc)
        status = _CATEGORY_STATUS.get(category, 500)
        body = {
            "ok": False,
            "request_id": request_id,
            "error": {
                "category": category,
                "message": str(exc),
            },
        }
        logger.warning(
            "request failed",
            extra={
                "event": "request_failed",
                "request_id": request_id,
                "category": category,
                "failure_reason": str(exc),
                "status_code": status,
            },
        )
        if trace is not None:
            trace.set_summary(status="failed", failure_category=category)
            app.state.traces.save(trace)
            body["error"]["trace"] = trace.as_dict()
        return JSONResponse(status_code=status, content=body)

    # ---------------- 健康检查 ----------------

    @app.get("/health")
    def health():
        return {"ok": True, "latest_version": app.state.store.latest_version()}

    # ---------------- 查询 ----------------

    @app.post("/query")
    def query(payload: QueryRequest, request: Request):
        request_id = header_or_new(request.headers.get("x-request-id"))
        set_request_id(request_id)
        trace = Trace(request_id)
        try:
            engine = Engine(
                app.state.store,
                block_size=app.state.settings.block_size,
                unknown_terms_empty=payload.unknown_terms_empty,
                logger=logger,
            )
            result = engine.execute(
                query=payload.query,
                version=payload.version,
                operand_order=payload.operand_order,
                request_id=request_id,
                trace_sink=trace,
            )
            app.state.traces.save(trace)
            return {
                "ok": True,
                "request_id": request_id,
                "version": result.version,
                "query": result.query,
                "result": {"count": len(result.doc_ids), "doc_ids": result.doc_ids},
                "stats": result.stats,
                "uncertainties": result.uncertainties,
                "trace": result.trace,
            }
        except QueryError as exc:  # parse_error / unknown_term
            return _error_response(exc, request_id, trace)
        except VersionNotFoundError as exc:
            return _error_response(exc, request_id, trace)
        except Exception as exc:  # noqa: BLE001 - 出口统一分类
            if not isinstance(exc, QueryError):
                logger.exception(
                    "internal error during query",
                    extra={"event": "internal_error", "request_id": request_id},
                )
            return _error_response(exc, request_id, trace)

    # ---------------- 版本管理 ----------------

    @app.get("/versions")
    def versions():
        return {
            "ok": True,
            "versions": [
                {
                    "version_id": v.version_id,
                    "parent_id": v.parent_id,
                    "created_at": v.created_at,
                    "message": v.message,
                    "doc_count": v.doc_count,
                    "visible_count": v.visible_count,
                }
                for v in app.state.store.list_versions()
            ],
        }

    @app.get("/versions/{version_id}")
    def version_detail(version_id: int):
        info = app.state.store.require_version(version_id)
        return {
            "ok": True,
            "version": {
                "version_id": info.version_id,
                "parent_id": info.parent_id,
                "created_at": info.created_at,
                "message": info.message,
                "doc_count": info.doc_count,
                "visible_count": info.visible_count,
                "universe": app.state.store.universe(version_id),
                "terms": app.state.store.known_terms(version_id),
                "events": app.state.store.events(version_id),
            },
        }

    @app.post("/versions/commit")
    def commit_version(payload: CommitRequest, request: Request):
        request_id = header_or_new(request.headers.get("x-request-id"))
        set_request_id(request_id)
        trace = Trace(request_id)
        try:
            adds = [(a.doc_id, a.terms) for a in payload.adds]
            new_id = app.state.store.commit(
                parent_id=payload.parent_version,
                adds=adds,
                deletes=payload.deletes,
                message=payload.message,
            )
            trace.add_step(
                "commit",
                parent_version=payload.parent_version,
                new_version=new_id,
                adds=len(adds),
                deletes=list(payload.deletes),
            )
            trace.set_summary(status="ok", new_version=new_id)
            app.state.traces.save(trace)
            logger.info(
                "version committed",
                extra={
                    "event": "version_committed",
                    "request_id": request_id,
                    "version": new_id,
                    "detail": {"adds": len(adds), "deletes": list(payload.deletes)},
                },
            )
            return {"ok": True, "request_id": request_id, "version_id": new_id}
        except (VersionNotFoundError, VersionConflictError) as exc:
            return _error_response(exc, request_id, trace)

    # ---------------- 索引读取 ----------------

    @app.get("/versions/{version_id}/terms/{term}")
    def get_posting(version_id: int, term: str):
        ids = app.state.store.posting(version_id, term)
        known = set(app.state.store.known_terms(version_id))
        return {
            "ok": True,
            "version": version_id,
            "term": term,
            "known": term in known,
            "count": len(ids),
            "doc_ids": ids,
        }

    # ---------------- 诊断 ----------------

    @app.get("/diagnostics/traces")
    def list_traces(limit: int = 20):
        return {"ok": True, "traces": app.state.traces.recent(limit)}

    @app.get("/diagnostics/traces/{request_id}")
    def get_trace(request_id: str):
        trace = app.state.traces.get(request_id)
        if trace is None:
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "request_id": request_id,
                    "error": {
                        "category": ErrorCategory.INTERNAL.value,
                        "message": f"未找到 request_id={request_id} 的轨迹（环形缓冲可能已淘汰）",
                    },
                },
            )
        return {"ok": True, "trace": trace.as_dict()}

    return app


# uvicorn app.postings_app:app 使用的默认应用实例
app = create_app()
