"""FastAPI 服务边界。

对外只暴露三类能力：

* ``POST /v1/fetch``          发起一次受保护抓取（内核全程裁决）；
* ``GET  /v1/audit/runs``     列出运行（可按 verdict 过滤）；
* ``GET  /v1/audit/runs/{id}`` 取某次运行的完整记录（含决策链）；
* ``GET  /v1/audit/verify``   重算哈希链/签名，证明审计未被篡改。

错误分类 → HTTP 状态的映射是显式契约：

    input_error          → 400
    policy_deny          → 403
    state_conflict       → 409
    resource_exhausted   → 429
    computation_failed   → 502

策略拒绝也会落审计（保留证据），但不向前端返回 200——调用方必须能据
状态码区分"成功取到内容"和"被安全策略拦下"。
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..audit.store import AuditLog
from ..errors import ErrorCategory, KernelError
from ..kernel import SecurityKernel
from ..net.connector import PinnedHTTPConnector
from ..net.resolver import FixtureResolver, parse_zone
from ..rules.loader import load_policy
from ..rules.policy import PolicyEngine

_HTTP_STATUS = {
    ErrorCategory.INPUT_ERROR: 400,
    ErrorCategory.POLICY_DENY: 403,
    ErrorCategory.STATE_CONFLICT: 409,
    ErrorCategory.RESOURCE_EXHAUSTED: 429,
    ErrorCategory.COMPUTATION_FAILED: 502,
}


class FetchRequest(BaseModel):
    url: str = Field(..., min_length=1, description="要抓取的绝对 URL")


def create_app(
    *,
    policy_path: str | None = None,
    zone_path: str | None = None,
    audit_db: str | None = None,
    kernel: SecurityKernel | None = None,
) -> FastAPI:
    app = FastAPI(title="safeproxy", version="1.0.0")

    audit_path = audit_db or os.environ.get(
        "AUDIT_DB", os.path.join("artifacts", "audit.sqlite3")
    )
    audit = AuditLog(audit_path)

    if kernel is None:
        p_path = policy_path or os.environ["POLICY_FILE"]
        z_path = zone_path or os.environ["ZONE_FILE"]
        bundle = load_policy(p_path)
        table = parse_zone(z_path)
        engine = PolicyEngine(bundle)
        resolver = FixtureResolver(table)
        connector = PinnedHTTPConnector()
        kernel = SecurityKernel(engine, resolver, connector, audit=audit)
    else:  # 测试注入：仍共享同一个审计库
        kernel._audit = audit  # type: ignore[attr-defined]

    app.state.audit = audit
    app.state.kernel = kernel

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/fetch")
    def fetch(req: FetchRequest) -> dict[str, Any]:
        result = kernel.fetch(req.url)
        err = result.get("error")
        if err is None:
            return result
        # 失败：按错误分类映射 HTTP 状态，body 仍含完整决策链与 run_id
        category = ErrorCategory(err["category"])
        status = _HTTP_STATUS[category]
        raise HTTPException(status_code=status, detail=result)

    @app.get("/v1/audit/runs")
    def list_runs(verdict: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"runs": audit.list_runs(limit=limit, verdict=verdict)}

    @app.get("/v1/audit/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        row = audit.get_run(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail={"run_id": run_id, "found": False})
        import json

        record = json.loads(row["record_json"])
        return {
            "run_id": row["run_id"],
            "verdict": row["verdict"],
            "url": row["url"],
            "status_code": row["status_code"],
            "error_code": row["error_code"],
            "error_category": row["error_cat"],
            "created_at": row["created_at"],
            "chain_hash": row["chain_hash"],
            "record": record,
        }

    @app.get("/v1/audit/verify")
    def verify() -> dict[str, Any]:
        try:
            return audit.verify_chain()
        except KernelError as exc:
            raise HTTPException(status_code=409, detail=exc.to_dict()) from exc

    return app
