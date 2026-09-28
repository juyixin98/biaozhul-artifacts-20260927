"""FastAPI 审计接口：提交/列出策略、提交证据、发起差分、查询结果与审计日志。

不提供用户/角色后台：调用方身份仅由签名与 actor 头标识，不做账号管理。
每个响应回带 request_id，便于与审计日志逐条关联。
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, Header, Request as FastAPIRequest
from fastapi.responses import JSONResponse

from .audit import Auditor
from .models import DiffAnalyzerError, Failure, FailureKind
from .service import PolicyService
from .store import Store


def create_app(service: PolicyService, store: Store, auditor: Auditor,
               *, max_trace_steps: int = 24) -> FastAPI:
    app = FastAPI(
        title="离线对象存储策略差分分析",
        version="1.0.0",
        description="默认拒绝 + 显式拒绝优先；三态判定；受限空间穷举见证。",
    )

    def _actor(actor: Optional[str]) -> str:
        # actor 仅用于审计关联，不是认证主体；未知时按匿名调用方记录
        return actor or "anonymous-caller"

    @app.exception_handler(DiffAnalyzerError)
    async def _on_domain_error(_req: FastAPIRequest, exc: DiffAnalyzerError):
        # 审计写入发生在各用例内；这里统一序列化失败类别
        return JSONResponse(
            status_code=_http_status(exc.kind),
            content={
                "failure": {
                    "kind": exc.kind.value,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    @app.get("/health")
    async def health():
        return {"status": "ok", "default_deny": True,
                "explicit_deny_precedence": True}

    @app.get("/v1/trust")
    async def trust():
        """公开信任锚点：系统承认哪些提交者公钥（不返回密钥本身）。"""
        return {"trusted_submitters": service.registry.submitters(),
                "scheme": "Ed25519", "user_backend": "none"}

    @app.post("/v1/policies")
    async def submit_policy(
        http: FastAPIRequest,
        x_actor: Optional[str] = Header(default=None),
        x_request_id: Optional[str] = Header(default=None),
    ):
        body = await http.json()
        request_id = x_request_id or auditor.new_request_id()
        actor = _actor(x_actor)
        auditor.event(request_id=request_id, actor=actor, component="api",
                      stage="POST /v1/policies", status="RECEIVED")
        try:
            policy = service.submit_policy(body, request_id=request_id, actor=actor)
        except DiffAnalyzerError as exc:
            auditor.failure(request_id=request_id, actor=actor,
                            component="api", stage="POST /v1/policies",
                            kind=exc.kind, message=exc.message, detail=exc.details)
            raise
        return JSONResponse({
            "request_id": request_id,
            "status": "ACCEPTED",
            "policy": {
                "version": policy.version,
                "source_hash": policy.source_hash,
                "submitted_by": policy.submitted_by,
                "rule_count": len(policy.rules),
            },
        })

    @app.get("/v1/policies")
    async def list_policies(x_actor: Optional[str] = Header(default=None)):
        return {"policies": store.list_policies()}

    @app.post("/v1/evidence")
    async def submit_evidence(
        http: FastAPIRequest,
        x_actor: Optional[str] = Header(default=None),
        x_request_id: Optional[str] = Header(default=None),
    ):
        body = await http.json()
        request_id = x_request_id or auditor.new_request_id()
        actor = _actor(x_actor)
        auditor.event(request_id=request_id, actor=actor, component="api",
                      stage="POST /v1/evidence", status="RECEIVED")
        try:
            out = service.submit_evidence(body, request_id=request_id, actor=actor)
        except DiffAnalyzerError as exc:
            auditor.failure(request_id=request_id, actor=actor,
                            component="api", stage="POST /v1/evidence",
                            kind=exc.kind, message=exc.message, detail=exc.details)
            raise
        return JSONResponse({"request_id": request_id, "status": "ACCEPTED", **out})

    @app.post("/v1/diffs")
    async def create_diff(
        http: FastAPIRequest,
        x_actor: Optional[str] = Header(default=None),
        x_request_id: Optional[str] = Header(default=None),
    ):
        body = await http.json()
        request_id = x_request_id or auditor.new_request_id()
        actor = _actor(x_actor)
        auditor.event(request_id=request_id, actor=actor, component="api",
                      stage="POST /v1/diffs", status="RECEIVED")
        try:
            result = service.run_diff(
                old_version=body["old_version"],
                new_version=body["new_version"],
                scope=body.get("scope", {}),
                request_id=request_id,
                actor=actor,
                evidence_bundle_id=body.get("evidence_bundle_id"),
            )
        except DiffAnalyzerError as exc:
            auditor.failure(request_id=request_id, actor=actor,
                            component="api", stage="POST /v1/diffs",
                            kind=exc.kind, message=exc.message, detail=exc.details)
            raise
        except KeyError as exc:
            err = DiffAnalyzerError(f"缺少必填字段: {exc}")
            auditor.failure(request_id=request_id, actor=actor,
                            component="api", stage="POST /v1/diffs",
                            kind=FailureKind.SCHEMA_INVALID, message=str(err))
            raise err
        return JSONResponse(_diff_response(request_id, result))

    @app.get("/v1/diffs/{diff_id}")
    async def get_diff(diff_id: str):
        row = store.get_diff(diff_id)
        if row is None:
            exc = DiffAnalyzerError("差分结果不存在")
            exc.kind = FailureKind.NOT_FOUND
            raise exc
        return {
            "diff_id": row["diff_id"],
            "request_id": row["request_id"],
            "actor": row["actor"],
            "old_version": row["old_version"],
            "new_version": row["new_version"],
            "scope": row["scope"],
            "summary": row["summary"],
            "witnesses": row["witnesses"],
            "evidence_report": row["evidence"],
        }

    @app.get("/v1/diffs")
    async def list_diffs():
        return {"diffs": store.list_diffs()}

    @app.get("/v1/audit")
    async def get_audit(
        request_id: Optional[str] = None,
        diff_id: Optional[str] = None,
        actor: Optional[str] = None,
        limit: int = 100,
    ):
        limit = max(1, min(limit, 500))
        events = store.query_audit(request_id=request_id, diff_id=diff_id,
                                   actor=actor, limit=limit)
        # 失败与不确定在响应中单列，便于审计人员直接定位
        failures = [e for e in events if e["status"].startswith("FAILURE")]
        inconclusive = [e for e in events if e["status"] == "INCONCLUSIVE"]
        return {
            "count": len(events),
            "failures": failures,
            "inconclusive": inconclusive,
            "events": events,
        }

    return app


def _http_status(kind: FailureKind) -> int:
    if kind is FailureKind.NOT_FOUND:
        return 404
    if kind in (FailureKind.SCHEMA_VERSION_CONFLICT,):
        return 409
    if kind.name.startswith("CRYPTO") or kind is FailureKind.EVIDENCE_TAMPERED:
        return 422
    return 400


def _diff_response(request_id: str, result) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "diff_id": result.diff_id,
        "old_version": result.old_version,
        "new_version": result.new_version,
        "scope": result.scope,
        "restricted_space": result.space,
        "summary": result.summary,
        "witnesses": result.witnesses,
        "evidence_report": result.evidence_report,
        "interpretation": {
            "verdict_meanings": {
                "WIDENED": "新版扩大了可访问请求集合（存在新增允许见证）",
                "WIDENED_WITH_UNKNOWN": "既存在确定的新增允许，也存在因未知条件"
                                        "而无法确定的空间点；不能据此整体放行",
                "SHRUNK": "存在新增拒绝见证",
                "SHRUNK_WITH_UNKNOWN": "存在新增拒绝，但同时有无法确定的空间点",
                "UNSURE_UNKNOWN": "存在因未知条件而无法确定的空间点，不得视为等价",
                "EQUIVALENT": "受限空间内两版判定完全一致",
            },
            "uncertainty_policy": "UNKNOWN 永远不按允许处理",
            "explicit_deny_precedence": True,
            "default_deny": True,
        },
    }
