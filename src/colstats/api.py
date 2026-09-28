"""FastAPI 验证接口。

端点：
- ``POST /audits``            审计文件，落库，返回结论与逐条发现；
- ``GET  /audits/{id}``       按 audit_id 取回带定位的诊断；
- ``GET  /audits``            列出最近审计；
- ``GET  /audits/by-request/{rid}`` 按请求标识检索；
- ``POST /query``             在文件上执行谓词查询（应用剪枝决策）；
- ``GET  /health``            健康检查。

每个请求都带 ``X-Request-ID``（缺省自动生成），诊断与落库记录均带该标识。
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field

from .config import Config, load_config
from .kernel import audit_file, can_prune
from .models import Claim
from .parquet_adapter import ParquetFormatError, parse_file
from .query import run_query
from .store import MetadataStore

log = logging.getLogger("colstats.api")


class AuditRequest(BaseModel):
    path: str = Field(..., description="本地 Parquet 文件路径")
    expose_values: bool | None = None


class QueryRequest(BaseModel):
    path: str
    column: str
    predicate: str
    value: Any = None
    value_high: Any = None
    audit_id: str | None = Field(
        None, description="复用已有审计结论；缺省时实时审计"
    )


class PruneRequest(BaseModel):
    min_claim: Any
    max_claim: Any
    physical_type: str
    predicate: str
    value: Any = None
    value_high: Any = None
    null_count: int | None = None
    num_values: int | None = None
    min_truncated: bool = False
    max_truncated: bool = False
    trusted: bool = True


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    logging.basicConfig(level=getattr(logging, cfg.log.level, logging.INFO))
    store = MetadataStore(cfg.service.db_path)
    app = FastAPI(
        title="列式文件统计审计后端",
        version="1.0.0",
        description=(
            "审计 Parquet 页级/列块级 min、max、NULL 计数与排序声明，"
            "并在坏统计被禁用剪枝后验证查询仍然正确。"
        ),
    )

    @app.middleware("http")
    async def add_request_id(request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    def _finding_dto(f) -> dict:
        return {
            "code": f.code,
            "severity": f.severity.value,
            "locator": f.locator,
            "message": f.message,
            "expected": f.expected,
            "observed": f.observed,
            "request_id": f.request_id,
        }

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "colstats-audit"}

    @app.post("/audits")
    def create_audit(body: AuditRequest, request: Request) -> dict:
        rid = request.state.request_id
        path = Path(body.path)
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"文件不存在: {body.path}")
        try:
            model = parse_file(path)
        except ParquetFormatError as exc:
            log.warning("request=%s 拒绝：%s", rid, exc)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        result = audit_file(
            model,
            supported_types=cfg.audit.supported_physical_types,
            expose_values=(
                body.expose_values
                if body.expose_values is not None
                else cfg.audit.expose_values
            ),
            request_id=rid,
        )
        store.save_audit(result, request_id=rid)
        log.info(
            "request=%s audit=%s path=%s verdict=%s errors=%d",
            rid, result.audit_id, body.path, result.verdict,
            result.summary["num_errors"],
        )
        return {
            "audit_id": result.audit_id,
            "request_id": rid,
            "path": result.path,
            "verdict": result.verdict,
            "summary": result.summary,
            "findings": [_finding_dto(f) for f in result.findings],
        }

    @app.get("/audits")
    def list_audits(limit: int = 50) -> dict:
        return {"audits": store.list_audits(limit=limit)}

    @app.get("/audits/by-request/{request_id}")
    def audits_by_request(request_id: str) -> dict:
        rows = store.find_by_request(request_id)
        if not rows:
            raise HTTPException(404, f"无请求标识 {request_id} 的审计")
        return {"request_id": request_id, "audits": rows}

    @app.get("/audits/{audit_id}")
    def get_audit(audit_id: str) -> dict:
        data = store.get_audit(audit_id)
        if data is None:
            raise HTTPException(404, f"无审计 {audit_id}")
        return data

    @app.post("/query")
    def query(body: QueryRequest, request: Request) -> dict:
        rid = request.state.request_id
        path = Path(body.path)
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"文件不存在: {body.path}")
        try:
            model = parse_file(path)
        except ParquetFormatError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        audit = None
        if body.audit_id:
            record = store.get_audit(body.audit_id)
            if record is None:
                raise HTTPException(404, f"无审计 {body.audit_id}")
        # 实时审计以取得 trusted 标志（审计结论用于剪枝门禁）
        audit = audit_file(
            model,
            supported_types=cfg.audit.supported_physical_types,
            expose_values=cfg.audit.expose_values,
            request_id=rid,
        )
        try:
            report = run_query(
                model, body.column, body.predicate, body.value,
                value_high=body.value_high, audit=audit,
            )
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        def _present(v: Any) -> Any:
            import math

            if isinstance(v, bytes):
                return v.hex()
            if isinstance(v, float):
                if math.isnan(v):
                    return "NaN-" if math.copysign(1.0, v) < 0 else "NaN+"
                return v
            return v

        return {
            "request_id": rid,
            "column": report.column,
            "predicate": report.predicate,
            "audit_verdict": report.audit_verdict,
            "trusted": report.trusted,
            "stats_would_miss_rows": report.stats_would_miss_rows,
            "groups": [
                {
                    "row_group": g.row_group,
                    "chunk_decision": g.chunk_decision,
                    "page_decisions": g.page_decisions,
                    "pruned_by_stats": g.pruned_by_stats,
                    "matched_rows": g.matched_rows,
                    "scanned_rows": g.scanned_rows,
                }
                for g in report.groups
            ],
            "num_results": len(report.result),
            "results": [_present(v) for v in report.result[:200]],
        }

    @app.post("/prune/check")
    def prune_check(body: PruneRequest) -> dict:
        """无文件依赖的纯决策端点：给定统计与谓词返回 PRUNE/SCAN/UNDECIDABLE。"""
        claim = Claim(
            has_min_max=True,
            null_count=body.null_count,
            has_null_count=body.null_count is not None,
            min_claim=body.min_claim,
            max_claim=body.max_claim,
            min_truncated=body.min_truncated,
            max_truncated=body.max_truncated,
            num_values=body.num_values if body.num_values is not None else 0,
        )
        decision = can_prune(
            claim, body.physical_type, body.predicate, body.value,
            trusted=body.trusted, value_high=body.value_high,
        )
        return {
            "decision": decision,
            "reason": {
                "PRUNE": "范围严格不相交，可安全跳过",
                "SCAN": "范围相交，必须扫描",
                "UNDECIDABLE": (
                    "统计缺失/被截断/不可信，规则禁止据此剪枝，必须扫描"
                ),
            }[decision],
        }

    return app


def main() -> None:  # pragma: no cover
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        create_app(cfg), host=cfg.service.host, port=cfg.service.port
    )


if __name__ == "__main__":  # pragma: no cover
    main()
