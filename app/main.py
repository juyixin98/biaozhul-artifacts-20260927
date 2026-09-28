"""FastAPI 服务入口。

路由：
- GET  /health                        健康/版本/条目数
- GET  /versions                      规范化版本、schema、修订号
- PUT  /entries/{id}                  单条 upsert（body: surface, score）
- POST /entries/bulk                  批量 upsert（整体校验、原子语义）
- POST /entries/{id}/score            直接设词频
- POST /entries/{id}/adjust           词频增减（可为负，结果不能为负）
- DELETE /entries/{id}                删除词条
- GET  /complete?prefix=&k=&trace=    精确 top-k 补全（可选剪枝 trace）
- POST /diagnostics/verify            上界/结构校验（deep=true 加 oracle 对拍）
- POST /snapshots                     创建持久快照
- GET  /snapshots                     列出快照
- POST /snapshots/{name}/restore      恢复快照并重建索引

错误语义见 README“错误码”一节；未知异常返回 500 E_INTERNAL（日志带堆栈），
绝不把异常包装成成功响应。
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from . import SCHEMA_VERSION, SERVICE_NAME
from .config import Settings
from .engine import Engine
from .errors import AppError
from .logging_setup import configure_logging
from .schemas import (
    AdjustIn,
    BatchIn,
    CompletionOut,
    EntryIn,
    EntryOut,
    EventOut,
    PruneOut,
    ScoreIn,
    SnapshotIn,
    TraceOut,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    logger, log_path = configure_logging(settings.log_dir, settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.engine = Engine(
            settings.db_path,
            settings.snapshot_dir,
            max_surface_len=settings.max_surface_len,
            max_limit=settings.max_limit,
            max_batch=settings.max_batch,
        )
        app.state.logger = logger
        logger.info(
            "engine ready",
            extra={
                "data": {
                    "service": SERVICE_NAME,
                    "schema_version": SCHEMA_VERSION,
                    **app.state.engine.versions(),
                    **app.state.engine.stats(),
                    "db": str(settings.db_path),
                }
            },
        )
        yield
        app.state.engine.close()
        logger.info("engine closed", extra={"data": {"log_file": str(log_path)}})

    app = FastAPI(title=SERVICE_NAME, version=SCHEMA_VERSION, lifespan=lifespan)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - 兜底：未知异常也要结构化 500
            logger.exception(
                "unhandled exception",
                extra={"data": {"request_id": request_id, "path": request.url.path}},
            )
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "request_id": request_id,
                    "error": {
                        "code": "E_INTERNAL",
                        "category": "internal",
                        "message": "服务器内部错误，详见服务日志",
                        "details": {},
                    },
                },
            )
        response.headers["x-request-id"] = request_id
        return response

    def _err(exc: AppError, request: Request) -> JSONResponse:
        rid = getattr(request.state, "request_id", None)
        logger.warning(
            "request failed: %s",
            exc.code,
            extra={
                "data": {
                    "request_id": rid,
                    "code": exc.code,
                    "path": request.url.path,
                    "details": exc.details,
                }
            },
        )
        return JSONResponse(status_code=exc.status_code, content=exc.to_body(rid))

    # 业务错误统一处理（在路由内抛出）。
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):  # noqa: RUF029
        return _err(exc, request)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):  # noqa: RUF029
        rid = getattr(request.state, "request_id", None)
        # 请求体/查询参数的 schema 校验失败：统一 400 E_INVALID_INPUT，
        # 并保留具体字段错误；绝不返回 FastAPI 默认的 422 非统一结构。
        logger.warning(
            "request validation failed",
            extra={"data": {"request_id": rid, "path": request.url.path,
                            "errors": exc.errors()}},
        )
        body = {
            "ok": False,
            "request_id": rid,
            "error": {
                "code": "E_INVALID_INPUT",
                "category": "invalid_input",
                "message": "请求体/参数校验失败",
                "details": {"errors": exc.errors()},
            },
        }
        return JSONResponse(status_code=400, content=body)

    def engine(request: Request) -> Engine:
        return request.app.state.engine

    # ---- 健康/版本 -------------------------------------------------------

    @app.get("/health")
    async def health(request: Request):
        eng = engine(request)
        return {
            "ok": True,
            "service": SERVICE_NAME,
            **eng.versions(),
            **eng.stats(),
        }

    @app.get("/versions")
    async def versions(request: Request):
        return {"ok": True, **engine(request).versions()}

    # ---- 写入 ------------------------------------------------------------

    @app.put("/entries/{entry_id}")
    async def upsert_entry(entry_id: str, payload: EntryIn, request: Request):
        # 路径 id 与 body id 同时存在时以路径为准，但冲突显式报错。
        if payload.id != entry_id:
            from .errors import InvalidSurface

            raise InvalidSurface(
                "路径 id 与 body.id 不一致",
                details={"path_id": entry_id, "body_id": payload.id},
            )
        entry = engine(request).upsert(entry_id, payload.surface, payload.score)
        request.app.state.logger.info(
            "upsert",
            extra={"data": {"request_id": request.state.request_id, "id": entry.id, "score": entry.score}},
        )
        return {"ok": True, "entry": EntryOut.from_entry(entry).model_dump()}

    @app.post("/entries/bulk")
    async def bulk(payload: BatchIn, request: Request):
        items = [m.model_dump() for m in payload.items]
        result = engine(request).upsert_batch(items)
        return {"ok": True, **result}

    @app.post("/entries/{entry_id}/score")
    async def set_score(entry_id: str, payload: ScoreIn, request: Request):
        entry = engine(request).set_score(entry_id, payload.score)
        return {"ok": True, "entry": EntryOut.from_entry(entry).model_dump()}

    @app.post("/entries/{entry_id}/adjust")
    async def adjust(entry_id: str, payload: AdjustIn, request: Request):
        entry = engine(request).adjust_score(entry_id, payload.delta)
        return {"ok": True, "entry": EntryOut.from_entry(entry).model_dump()}

    @app.delete("/entries/{entry_id}")
    async def delete_entry(entry_id: str, request: Request):
        engine(request).delete(entry_id)
        return {"ok": True, "deleted": entry_id}

    # ---- 查询 ------------------------------------------------------------

    @app.get("/complete", response_model=CompletionOut)
    async def complete(
        request: Request,
        prefix: str = Query("", description="原始前缀，服务端做规范化；空前缀=全量 top-k"),
        k: int = Query(10, ge=1, le=1000),
        trace: bool = Query(False, description="返回剪枝依据与逐步 trace"),
    ):
        result = engine(request).complete(prefix, k, trace=trace)
        out = CompletionOut(
            prefix=result.prefix,
            normalized_prefix=result.normalized_prefix,
            k=result.k,
            count=len(result.entries),
            entries=[EntryOut.from_entry(e) for e in result.entries],
        )
        if trace:
            tr = result.trace
            out.trace = TraceOut(
                location=tr.location_kind,
                stats={
                    "nodes_visited": tr.stats.nodes_visited,
                    "subtrees_pruned": tr.stats.subtrees_pruned,
                    "entries_seen": tr.stats.entries_seen,
                    "frontier_expansions": tr.stats.frontier_expansions,
                    "total_nodes": tr.stats.total_nodes,
                    "items_evicted": tr.stats.items_evicted,
                },
                prunes=[
                    PruneOut(
                        subtree_prefix=p,
                        upper_bound=r.upper_bound,
                        best_k_score=r.best_k_score,
                        justification=(
                            f"子树上界 {r.upper_bound} 是该子树所有词频的最大值（可靠上界）；"
                            f"它严格小于当前第 {k} 名分数 {r.best_k_score}，"
                            "故整棵子树不可能含更好候选，安全剪枝"
                        ),
                    )
                    for p, r in tr.prunes
                ],
                events=[
                    EventOut(
                        step=ev.step,
                        event=ev.event,
                        node_seq=ev.node_seq,
                        edge_seq=ev.edge_seq,
                        edge_label=ev.edge_label,
                        prefix=ev.prefix,
                        upper_bound=ev.upper_bound,
                        detail=ev.detail,
                    )
                    for ev in tr.events
                ],
            )
        return out

    # ---- 诊断 ------------------------------------------------------------

    @app.post("/diagnostics/verify")
    async def diagnostics_verify(
        request: Request,
        deep: bool = Query(False),
        prefixes: list[str] | None = Query(None),
    ):
        result = engine(request).verify(deep=deep, sample_prefixes=prefixes)
        return {"ok": True, **result}

    # ---- 快照 ------------------------------------------------------------

    @app.post("/snapshots")
    async def create_snapshot(payload: SnapshotIn, request: Request):
        record = engine(request).create_snapshot(payload.name, payload.note)
        return {"ok": True, "snapshot": record}

    @app.get("/snapshots")
    async def list_snapshots(request: Request):
        return {"ok": True, "snapshots": engine(request).list_snapshots()}

    @app.post("/snapshots/{name}/restore")
    async def restore_snapshot(name: str, request: Request):
        result = engine(request).restore_snapshot(name)
        return {"ok": True, **result}

    return app


# uvicorn app.main:app
app = create_app()
