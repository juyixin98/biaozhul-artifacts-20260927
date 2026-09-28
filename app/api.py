"""HTTP 查询与诊断接口（FastAPI）。

错误语义
========

==============================  ======  ========================================
情况                            状态码  error_code
==============================  ======  ========================================
参数校验失败（Pydantic）        400     VALIDATION_ERROR
空词条 / NaN 分 / 非法 k        400     VALIDATION_ERROR
词条不存在（删除）              404     ENTRY_NOT_FOUND
版本/快照不存在                 404     VERSION_NOT_FOUND
规范化版本不兼容                409     NORMALIZER_MISMATCH
索引与存储不一致（降级）        500     INDEX_DEGRADED
其他未预期异常                  500     INTERNAL_ERROR（带 request_id，写日志）
==============================  ======  ========================================

未知异常绝不返回成功：统一记日志（含 request_id 与堆栈），响应只带错误 ID。
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import __version__
from .config import Settings
from .engine import Engine
from .errors import DomainError
from .normalize import NORMALIZER_VERSION
from .schemas import (
    BulkUpsertIn,
    BulkUpsertResponse,
    CompletionOut,
    CompletionResponse,
    DeleteIn,
    DeleteResponse,
    RestoreIn,
    RestoreResponse,
    SnapshotIn,
    SnapshotResponse,
    VersionInfo,
)

logger = logging.getLogger("ctrie")


def create_app(settings: Optional[Settings] = None, engine: Optional[Engine] = None) -> FastAPI:
    """构造应用。测试可注入自己的 engine（指向临时数据库）。"""
    settings = settings or Settings.from_env()
    app = FastAPI(
        title="Unicode 压缩 Trie 补全后端",
        version=__version__,
        description="规范化版本固定、子树可靠上界、精确 top-k 的前缀补全服务。",
    )
    app.state.settings = settings
    app.state.engine = engine or Engine(settings.db_path, topk_max=settings.topk_max)

    def eng() -> Engine:
        return app.state.engine

    # ------------------------------------------------------------- 错误处理

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        logger.warning("[%s] 领域错误 %s: %s", rid, exc.error_code, exc)
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "error_code": exc.error_code,
                "message": str(exc),
                "request_id": rid,
                "version": eng().storage.head_version,
                "normalizer_version": NORMALIZER_VERSION,
                "field": getattr(exc, "field", None),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        errors = exc.errors()
        logger.warning("[%s] 请求参数校验失败: %s", rid, errors)
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "VALIDATION_ERROR",
                "message": "请求参数校验失败",
                "request_id": rid,
                "version": eng().storage.head_version,
                "normalizer_version": NORMALIZER_VERSION,
                "details": errors,
            },
        )

    @app.exception_handler(ValidationError)
    async def pydantic_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        errors = exc.errors()
        logger.warning("[%s] 请求体校验失败: %s", rid, errors)
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "VALIDATION_ERROR",
                "message": "请求参数校验失败",
                "request_id": rid,
                "details": errors,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        # 未知异常：完整堆栈进日志，响应只暴露错误 ID —— 不伪装成功。
        logger.exception("[%s] 未预期异常: %s", rid, exc)
        return JSONResponse(
            status_code=500,
            content={
                "error_code": "INTERNAL_ERROR",
                "message": "服务内部错误，请凭 request_id 联系运维查日志",
                "request_id": rid,
            },
        )

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        logger.info(
            "[%s] %s %s 开始 head=v%s",
            request.state.request_id,
            request.method,
            request.url.path,
            eng().storage.head_version,
        )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        logger.info(
            "[%s] %s %s -> %s",
            request.state.request_id,
            request.method,
            request.url.path,
            response.status_code,
        )
        return response

    # ----------------------------------------------------------------- 接口

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service_version": __version__,
                "normalizer_version": NORMALIZER_VERSION}

    @app.get("/api/v1/status")
    async def status() -> dict:
        return eng().status()

    @app.get("/api/v1/complete")
    async def complete(
        prefix: str = "",
        k: int = 10,
        version: Optional[int] = None,
        diagnostics: bool = False,
    ) -> dict:
        result = eng().complete(prefix, k=k, version=version, diagnostics=diagnostics)
        out = [
            {"term": display, "id": eid, "score": score, "term_norm": term}
            for term, display, eid, score in result.rows
        ]
        body = {
            "query_prefix": prefix,
            "prefix_norm": result.prefix_norm,
            "normalizer_version": NORMALIZER_VERSION,
            "version": result.version,
            "k": k,
            "count": len(out),
            "results": out,
        }
        if diagnostics and result.trace is not None:
            body["diagnostics"] = _trace_to_dict(result.trace)
        return body

    @app.post("/api/v1/entries:bulkUpsert", response_model=BulkUpsertResponse)
    async def bulk_upsert(body: BulkUpsertIn) -> BulkUpsertResponse:
        entries = [e.model_dump() for e in body.entries]
        version_id, inserted, updated, batch_id = eng().bulk_upsert(
            entries, client_batch_id=body.client_batch_id, note=body.note
        )
        return BulkUpsertResponse(
            version=version_id,
            parent_version=version_id - 1,
            client_batch_id=batch_id,
            inserted=inserted,
            updated=updated,
            entry_count=eng().storage.count_entries(),
        )

    @app.post("/api/v1/entries:delete", response_model=DeleteResponse)
    async def delete_entry(body: DeleteIn) -> DeleteResponse:
        version_id, _ok = eng().delete_entry(body.id)
        return DeleteResponse(
            version=version_id, id=body.id, deleted=True,
            entry_count=eng().storage.count_entries(),
        )

    @app.get("/api/v1/versions", response_model=list[VersionInfo])
    async def list_versions(limit: int = 100) -> list[VersionInfo]:
        limit = max(1, min(limit, 1000))
        return [VersionInfo(**v) for v in eng().versions(limit)]

    @app.post("/api/v1/snapshots", response_model=SnapshotResponse)
    async def create_snapshot(body: SnapshotIn) -> SnapshotResponse:
        info = eng().snapshot(body.note)
        return SnapshotResponse(**info)

    @app.get("/api/v1/snapshots")
    async def list_snapshots() -> dict:
        return {"snapshots": eng().snapshots()}

    @app.post("/api/v1/snapshots:restore", response_model=RestoreResponse)
    async def restore_snapshot(body: RestoreIn) -> RestoreResponse:
        info = eng().restore(body.version, body.note)
        return RestoreResponse(**info)

    @app.get("/api/v1/diagnostics/invariants")
    async def invariants() -> dict:
        violations = eng().check_invariants()
        return {"healthy": not violations, "violations": violations}

    return app


def _trace_to_dict(trace) -> dict:
    return {
        "prefix_norm": trace.prefix_norm,
        "matched": trace.matched,
        "node_id": trace.node_id,
        "pushed": trace.pushed,
        "expanded": trace.expanded,
        "terminals_seen": trace.terminals_seen,
        "terminal_skipped_by_bound": trace.terminal_skipped_by_bound,
        "pruned_children": trace.pruned_children,
        "heap_pops": trace.heap_pops,
        "decisions": [
            {
                "node_id": d.node_id,
                "edge_label": d.edge_label,
                "subtree_best_score": d.subtree_best_score,
                "threshold_score": d.threshold_score,
                "decision": d.decision,
                "reason": d.reason,
            }
            for d in trace.decisions
        ],
    }
