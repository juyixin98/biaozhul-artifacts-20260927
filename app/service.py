"""FastAPI 服务层：请求身份、错误分类、版本存储接口与诊断输出。"""
from __future__ import annotations

import threading
from typing import Optional

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field, field_validator

from .config import Settings, load_settings
from .costs import CostProfile, cost_profile_from_dict, load_cost_profile
from .index import LexiconIndex
from .lexicon import (
    VersionError,
    VersionNotFound,
    activate_version,
    connect,
    create_version,
    fetch_entries,
    get_active_version,
    list_versions,
)
from .logging_setup import (
    configure_logging,
    get_logger,
    get_request_id,
    new_request_id,
    set_request_id,
)
from .query import QueryRejected, correct

log = get_logger()


class CostOverride(BaseModel):
    insert: Optional[float] = None
    delete: Optional[float] = None
    substitute: Optional[float] = None
    transpose: Optional[float] = None
    substitute_table: Optional[dict[str, dict[str, float]]] = None


class CorrectRequest(BaseModel):
    query: str = Field(..., description="待纠错文本")
    threshold: Optional[float] = Field(None, description="保留距离 <= threshold 的候选")
    max_results: Optional[int] = Field(None, ge=1, le=200)
    costs: Optional[CostOverride] = Field(None, description="单次请求代价覆盖（非负）")

    @field_validator("query")
    @classmethod
    def _query_not_literal_empty(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("query 必须是字符串")
        return v

    @field_validator("threshold")
    @classmethod
    def _threshold_nonneg(cls, v):
        if v is not None and v < 0:
            raise ValueError("threshold 必须非负")
        return v


class CreateVersionRequest(BaseModel):
    entries: list[dict] = Field(..., description="[{word, freq}, ...]")
    version_id: Optional[str] = None
    source: str = "api"
    activate: bool = True


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    configure_logging()
    settings = settings or load_settings()
    app = FastAPI(
        title="加权编辑距离纠错服务",
        version="1.0.0",
        description="非限制性 Damerau-Levenshtein（Lowrance-Wagner）候选纠错",
    )
    app.state.settings = settings
    app.state.conn = connect(settings.db_file)
    app.state.index_cache: dict[str, LexiconIndex] = {}
    app.state.lock = threading.Lock()

    def profile_override(req: CorrectRequest) -> CostProfile:
        base = load_cost_profile().to_public_dict()
        if req.costs is not None:
            # Pydantic v2 属性名以 model_ 开头时需用 object.__getattribute__ 访问
            over = object.__getattribute__(req.costs, "model_dump")(exclude_none=True)
            if "substitute_table" in over:
                merged = {k: dict(v) for k, v in base["substitute_table"].items()}
                for src, row in over["substitute_table"].items():
                    merged.setdefault(src, {}).update(row)
                over["substitute_table"] = merged
            base.update(over)
        return cost_profile_from_dict(base)

    def active_index() -> LexiconIndex:
        conn = app.state.conn
        with app.state.lock:
            vid = get_active_version(conn)
            if vid is None:
                raise QueryRejected(
                    "no_active_version", "词典中没有激活版本，请先创建或激活版本"
                )
            cache: dict[str, LexiconIndex] = app.state.index_cache
            if vid not in cache:
                cache[vid] = LexiconIndex(vid, fetch_entries(conn, vid))
        return cache[vid]

    @app.middleware("http")
    async def bind_request_id(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or new_request_id()
        set_request_id(request_id)
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    @app.get("/health")
    def health():
        conn = app.state.conn
        vid = get_active_version(conn)
        return {"status": "ok", "active_version": vid, "request_id": get_request_id()}

    @app.get("/versions")
    def versions():
        return {"versions": list_versions(app.state.conn)}

    @app.post("/versions")
    def create_ver(req: CreateVersionRequest):
        entries = [(e["word"], int(e.get("freq", 0))) for e in req.entries]
        try:
            vid = create_version(
                app.state.conn,
                entries,
                version_id=req.version_id,
                source=req.source,
                activate=req.activate,
            )
        except VersionError as exc:
            return _error(409, "version_error", str(exc))
        app.state.index_cache.pop(vid, None)
        log.info("version created", extra={"event": "version_created", "version_id": vid,
                                           "detail": f"{len(entries)} entries"})
        return {"version_id": vid, "entry_count": len(entries), "activated": req.activate}

    @app.post("/versions/{version_id}/activate")
    def activate(version_id: str):
        try:
            activate_version(app.state.conn, version_id)
        except VersionNotFound as exc:
            return _error(404, "version_not_found", str(exc))
        return {"version_id": version_id, "activated": True}
    @app.post("/v1/correct")
    def correct_endpoint(req: CorrectRequest):
        s: Settings = app.state.settings
        try:
            profile = profile_override(req)
            index = active_index()
            result = correct(
                req.query,
                profile=profile,
                index=index,
                threshold=req.threshold,
                max_results=req.max_results,
                max_query_length=s.max_query_length,
                max_candidates_evaluated=s.max_candidates_evaluated,
                default_max_results=s.max_results,
                uncertainty_margin=s.uncertainty_margin,
            )
            result["request_id"] = get_request_id()
            log.info(
                "correct ok",
                extra={
                    "event": "correct",
                    "version_id": index.version_id,
                    "detail": f"{result['result_count']} results",
                },
            )
            return result
        except QueryRejected as rej:
            return _error(422, rej.code, rej.message)
        except (ValueError, VersionError) as exc:
            return _error(422, "invalid_request", str(exc))

    def _error(status: int, code: str, message: str):
        log.info(
            "request failed",
            extra={"event": "request_failed", "failure_code": code, "detail": message},
        )
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=status,
            content={
                "error": True,
                "failure_code": code,
                "message": message,
                "request_id": get_request_id(),
            },
        )

    return app


app = create_app()
