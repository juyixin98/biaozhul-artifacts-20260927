"""FastAPI 应用工厂与路由。

端点：
- POST /api/v1/redact              整段脱敏
- POST /api/v1/stream/open         打开流式会话
- POST /api/v1/stream/chunk        发送流式块（可 final=true 收尾）
- POST /api/v1/stream/finalize     显式收尾
- GET  /api/v1/healthz             健康检查（规则档与版本）
- GET  /api/v1/requests/{rid}      公开元数据
- GET  /api/v1/requests            列表
- GET  /api/v1/audit/requests/{rid}        令牌：详情（输出/映射/步骤/不确定）
- GET  /api/v1/audit/requests/{rid}/mappings/{i}/original  令牌：单条原文
"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from .. import __version__
from ..audit import AuditDenied, AuditService
from ..config import Settings, get_settings
from ..core.redactor import RedactionResult
from ..rules.parser import Registry, RuleConfigError, load_registry
from ..services.redaction_service import RedactionService, ServiceError
from ..state.audit_store import AuditError, AuditStore
from ..state.sessions import RequestState
from .schemas import (
    RedactionResponse,
    RejectionOut,
    StreamChunkRequest,
    StreamChunkResponse,
    StreamOpenRequest,
    StreamOpenResponse,
    UncertaintyOut,
    WholeRedactRequest,
)


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    try:
        registry = load_registry(settings.rules_path)
    except RuleConfigError as exc:
        raise RuntimeError(f"规则档加载失败，拒绝启动: {exc}") from exc
    store = AuditStore(settings.db_path, settings.audit_key)
    service = RedactionService(registry, store,
                               max_chars=settings.max_request_chars)
    audit_service = AuditService(store, settings.audit_token)

    app = FastAPI(
        title="日志脱敏服务（合成数据）",
        version=__version__,
        description="按字段与模式规则的跨块日志脱敏，保留加密原文位置映射。",
    )
    app.state.settings = settings
    app.state.registry = registry
    app.state.store = store
    app.state.service = service
    app.state.audit = audit_service

    # ------------------------------------------------------------------ #
    def result_to_response(state: RequestState,
                           result: RedactionResult) -> RedactionResponse:
        return RedactionResponse(
            request_id=state.request_id,
            status=result.status,  # type: ignore[arg-type]
            redacted_text=result.output,
            profile=result.profile_name,
            profile_version=result.profile_version,
            engine_version=result.engine_version,
            original_length=result.original_length,
            output_length=result.output_length,
            mappings=[
                {
                    "index": i,
                    "rule_id": m.rule_id,
                    "source": m.source,
                    "key": m.key,
                    "original_span": [m.original_start, m.original_end],
                    "output_span": [m.output_start, m.output_end],
                    "replacement": m.replacement,
                    "original_sha256": m.original_sha256,
                }
                for i, m in enumerate(result.mappings)
            ],
            uncertainties=[
                UncertaintyOut(code=u.code, start=u.start, end=u.end,
                               detail=u.detail)
                for u in result.uncertainties
            ],
            rejected=[
                RejectionOut(
                    winner=rj.winner.rule_id, loser=rj.loser.rule_id,
                    winner_span=[rj.winner.start, rj.winner.end],
                    loser_span=[rj.loser.start, rj.loser.end],
                    reason=rj.reason)
                for rj in result.rejected
            ],
            residual_findings=result.residual_findings,
            error_code=result.error_code,
            error_message=result.error_message,
            steps=[
                {"kind": e.kind, **e.payload} for e in result.events
            ],
        )

    def raise_service(exc: ServiceError) -> None:
        raise HTTPException(status_code=exc.http_status, detail={
            "error_code": exc.code, "message": str(exc)})

    # ------------------------------------------------------------------ #
    @app.get("/api/v1/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "engine_version": __version__,
            "default_profile": registry.default_profile,
            "profiles": sorted(registry.profiles),
            "active_sessions": service.sessions.active_count(),
        }

    @app.post("/api/v1/redact", response_model=RedactionResponse)
    def redact_whole(req: WholeRedactRequest) -> RedactionResponse:
        try:
            state = service.redact_whole(req.text, req.profile)
        except ServiceError as exc:
            raise_service(exc)
        assert state.result is not None
        return result_to_response(state, state.result)

    @app.post("/api/v1/stream/open", response_model=StreamOpenResponse)
    def stream_open(req: StreamOpenRequest) -> StreamOpenResponse:
        try:
            state = service.open_stream(req.profile)
        except ServiceError as exc:
            raise_service(exc)
        return StreamOpenResponse(
            request_id=state.request_id,
            profile=state.profile_name,
            profile_version=state.profile_version,
        )

    @app.post("/api/v1/stream/chunk", response_model=StreamChunkResponse)
    def stream_chunk(req: StreamChunkRequest) -> StreamChunkResponse:
        try:
            state, emitted, result = service.feed_stream(
                req.request_id, req.chunk, is_final=req.final)
        except ServiceError as exc:
            raise_service(exc)
        return StreamChunkResponse(
            request_id=state.request_id,
            emitted_text=emitted.text if emitted else "",
            emitted_original_span=[
                emitted.original_start if emitted else 0,
                emitted.original_end if emitted else 0],
            held_chars=state.redactor.held_chars,
            finalized=state.finalized,
            result=result_to_response(state, result) if result else None,
        )

    @app.post("/api/v1/stream/finalize", response_model=RedactionResponse)
    def stream_finalize(request_id: str = Query(...)) -> RedactionResponse:
        try:
            state = service.finalize_stream(request_id)
        except ServiceError as exc:
            raise_service(exc)
        assert state.result is not None
        return result_to_response(state, state.result)

    # ------------------------------------------------------------------ #
    def audit_token(
        x_audit_token: str | None = Header(default=None,
                                          alias="X-Audit-Token"),
    ) -> str:
        if not x_audit_token:
            raise HTTPException(status_code=401, detail={
                "error_code": "AUDIT_TOKEN_MISSING",
                "message": "需要 X-Audit-Token 头"})
        return x_audit_token

    @app.get("/api/v1/requests/{request_id}")
    def public_summary(request_id: str) -> dict[str, Any]:
        try:
            return audit_service.request_summary(request_id)
        except AuditDenied as exc:
            raise HTTPException(status_code=404, detail={
                "error_code": exc.reason, "message": "请求不存在"})

    @app.get("/api/v1/requests")
    def public_list(limit: int = 50) -> dict[str, Any]:
        return {"requests": audit_service.list_requests(limit=limit)}

    @app.get("/api/v1/audit/requests/{request_id}")
    def audit_detail(request_id: str,
                     token: str = Depends(audit_token)) -> dict[str, Any]:
        try:
            return audit_service.request_detail(request_id, token)
        except AuditDenied as exc:
            status = 404 if exc.reason == "REQUEST_NOT_FOUND" else 403
            raise HTTPException(status_code=status, detail={
                "error_code": exc.reason, "message": exc.reason})

    @app.get("/api/v1/audit/requests/{request_id}/mappings/{index}/original")
    def audit_original(request_id: str, index: int,
                       token: str = Depends(audit_token)) -> dict[str, Any]:
        try:
            return audit_service.mapping_original(request_id, index, token)
        except AuditDenied as exc:
            status = 404 if exc.reason in ("REQUEST_NOT_FOUND",
                                           "MAPPING_INDEX_OUT_OF_RANGE") else 403
            raise HTTPException(status_code=status, detail={
                "error_code": exc.reason, "message": exc.reason})

    return app


app = build_app()
