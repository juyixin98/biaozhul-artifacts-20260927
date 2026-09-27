"""FastAPI 路由。

所有结果与失败都带 request_id；失败走统一错误信封（category 单列），
不确定结论走 warnings/uncertainty 字段，二者不与正常结果混排。
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from ..query import planner as PL
from ..query.engine import QueryEngine
from ..storage.version_store import VersionStore
from ..text import spec
from .service_error import ServiceError

router = APIRouter()


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class CommitBody(BaseModel):
    adds: dict[int, str] = Field(default_factory=dict)
    deletes: list[int] = Field(default_factory=list)
    note: str | None = None


def get_store(request: Request) -> VersionStore:
    return request.app.state.store


def get_engine(request: Request) -> QueryEngine:
    return QueryEngine(request.app.state.store)


def _finish_outcome(outcome, request: Request, expression: str):
    """统一把引擎结果落 trace、写日志；失败转成 ServiceError 交异常处理器。"""
    rid = request.state.request_id
    trace = {
        "request_id": rid,
        "started_at": request.state.started_at,
        "version": outcome.version,
        "expression": expression,
        "status": "ok" if outcome.ok else "error",
        "error_category": getattr(outcome, "error_category", None),
        "error_message": getattr(outcome, "error_message", None),
        "result_count": getattr(outcome, "count", None) if outcome.ok else None,
        "stats": getattr(outcome, "stats", None) if outcome.ok else None,
        "steps": getattr(outcome, "steps", None) if outcome.ok else None,
    }
    request.app.state.store.save_trace(trace)
    if outcome.ok:
        request.app.state.json_log.request_done(
            rid,
            status="ok",
            version=outcome.version,
            expression=expression,
            result_count=outcome.count,
            stats=outcome.stats,
            short_circuited=getattr(outcome, "short_circuited", None),
        )
        return outcome.to_response()

    request.app.state.json_log.request_done(
        rid,
        status="error",
        version=outcome.version,
        expression=expression,
        result_count=None,
        error_category=outcome.error_category,
        error_message=outcome.error_message,
    )
    raise ServiceError.from_outcome(outcome, expression=expression)


# ---------------------------------------------------------------------------
# 基础路由
# ---------------------------------------------------------------------------


@router.get("/health")
def health(request: Request) -> dict:
    store = get_store(request)
    return {
        "ok": True,
        "latest_version": store.latest_version(),
        "block_size": store.block_size,
        "db": store.db_path,
    }


@router.get("/spec")
def grammar() -> dict:
    """返回文本规范与失败类别清单。"""
    return spec.grammar_spec()


@router.get("/terms")
def list_terms(
    request: Request,
    prefix: str | None = None,
    limit: int = Query(default=200, ge=1, le=10_000),
) -> dict:
    store = get_store(request)
    terms = store.list_terms(prefix=prefix, limit=limit)
    return {"ok": True, "terms": terms, "count": len(terms)}


@router.get("/versions")
def list_versions(request: Request) -> dict:
    store = get_store(request)
    versions = store.versions()
    return {"ok": True, "versions": versions, "latest": store.latest_version()}


@router.post("/versions/commit", status_code=201)
def commit(body: CommitBody, request: Request) -> dict:
    store = get_store(request)
    try:
        version = store.commit(adds=body.adds, deletes=body.deletes, note=body.note)
    except Exception as e:
        raise ServiceError(
            "storage_error", f"{type(e).__name__}: {e}", http_status=400
        ) from e
    return {
        "ok": True,
        "request_id": request.state.request_id,
        "version": version,
        "universe_size": len(store.universe(version)),
    }


# ---------------------------------------------------------------------------
# 查询路由（核心）
# ---------------------------------------------------------------------------


@router.get("/query")
def query(
    request: Request,
    expr: str = Query(..., description="布尔表达式，如 a AND NOT b"),
    version: int | None = Query(None, ge=0),
    order: str = Query(PL.ORDER_RARE_FIRST),
):
    outcome = get_engine(request).query(
        expr, version=version, order=order, request_id=request.state.request_id
    )
    return _finish_outcome(outcome, request, expr)


@router.get("/explain")
def explain(
    request: Request,
    expr: str,
    version: int | None = Query(None, ge=0),
):
    outcome = get_engine(request).explain(
        expr, version=version, request_id=request.state.request_id
    )
    return _finish_outcome(outcome, request, expr)


# ---------------------------------------------------------------------------
# 诊断路由
# ---------------------------------------------------------------------------


@router.get("/diagnostics/requests")
def recent_requests(request: Request, limit: int = Query(20, ge=1, le=200)) -> dict:
    store = get_store(request)
    return {
        "ok": True,
        "request_id": request.state.request_id,
        "traces": store.recent_traces(limit=limit),
    }


@router.get("/diagnostics/requests/{request_id}")
def get_request(request_id: str, request: Request) -> dict:
    store = get_store(request)
    trace = store.get_trace(request_id)
    if trace is None:
        raise ServiceError(
            "trace_not_found",
            f"找不到请求 {request_id!r} 的诊断记录",
            http_status=404,
        )
    return {"ok": True, "request_id": request.state.request_id, "trace": trace}
