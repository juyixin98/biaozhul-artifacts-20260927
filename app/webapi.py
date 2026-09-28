"""FastAPI 接口：代理调用入口 + 审计查询/校验。

路由：

* ``GET  /health``
* ``POST /v1/fetch``          body: {url, grants?} -> RunResult（决策链）
* ``GET  /v1/runs?limit=&verdict=``
* ``GET  /v1/runs/{run_id}``  完整证据链
* ``GET  /v1/runs/{run_id}/verify``  Ed25519 签名校验
* ``GET  /v1/key``            审计签名公钥

失败类别 -> HTTP 状态映射::

    input_error         400
    policy_denied       403
    state_conflict      409
    resource_exhausted  508
    computation_failed  502

注意：被策略拒绝的请求**仍返回完整决策链**（body.verdict == "deny"），
HTTP 状态表达类别，证据用于复核。
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .audit import AuditStore
from .contracts import FailureKind, RunResult
from .kernel import GuardKernel
from .policy import Policy
from .resolver import ControlledResolver

_HTTP_STATUS = {
    FailureKind.INPUT_ERROR.value: 400,
    FailureKind.POLICY_DENIED.value: 403,
    FailureKind.STATE_CONFLICT.value: 409,
    FailureKind.RESOURCE_EXHAUSTED.value: 508,
    FailureKind.COMPUTATION_FAILED.value: 502,
}


class FetchRequest(BaseModel):
    url: str = Field(..., description="要代理访问的绝对 http(s) URL")
    grants: list[dict[str, Any]] | None = Field(
        None, description="本次请求专属的运行期放行规则（仅本 run 生效）"
    )
    max_redirects: int | None = Field(None, ge=0, le=20)
    max_bytes: int | None = Field(None, ge=1, le=16 * 1024 * 1024)


class KernelFactory:
    """每次请求构造独立 kernel/策略克隆 —— 运行间状态隔离。"""

    def __init__(self, *, resolver: ControlledResolver, base_policy: Policy,
                 audit: AuditStore, tls_context=None, defaults: dict[str, Any]) -> None:
        self.resolver = resolver
        self.base_policy = base_policy
        self.audit = audit
        self.tls_context = tls_context
        self.defaults = defaults

    def build(self, req: FetchRequest) -> GuardKernel:
        # resolver.clone() 让脚本计数等易变状态不跨 run 泄漏
        resolver = self.resolver.clone()
        grants = self._materialize_grants(req.grants, resolver)
        policy = self.base_policy.with_grants(grants)
        return GuardKernel(
            resolver=resolver,
            policy=policy,
            max_redirects=req.max_redirects if req.max_redirects is not None
            else self.defaults["max_redirects"],
            max_bytes=req.max_bytes if req.max_bytes is not None
            else self.defaults["max_bytes"],
            timeout_s=self.defaults["timeout_s"],
            tls_context=self.tls_context,
        )

    @staticmethod
    def _materialize_grants(grants, resolver: ControlledResolver) -> list[dict[str, Any]]:
        """grant 中可用 {"host": h, "records": [ips...]} 顺带注入一次性 DNS 区域，
        这样演示无需改全局 zones 文件。返回纯规则列表。"""
        if not grants:
            return []
        rules: list[dict[str, Any]] = []
        for g in grants:
            if "records" in g:
                resolver.add_zone(g["host"], {"records": list(g["records"])})
            rules.append({
                "id": g.get("id", f"grant-{g.get('host', 'x')}"),
                "action": "allow",
                "scheme": g.get("scheme"),
                "host": g.get("host"),
                "port": g.get("port"),
                "tag_any": g.get("tag_any", []),
                "note": g.get("note", "per-request grant"),
            })
        return rules


def create_app(*, kernel_factory: KernelFactory) -> FastAPI:
    app = FastAPI(title="SSRF Guard Demo", version="1.0.0")
    app.state.factory = kernel_factory
    audit: AuditStore = kernel_factory.audit
    app.state.audit = audit

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/v1/fetch")
    def fetch(req: FetchRequest):
        kernel = app.state.factory.build(req)
        result: RunResult = kernel.fetch(req.url, audit_sink=audit)
        body = result.to_dict()
        if result.status == "completed":
            return JSONResponse(status_code=200, content=body)
        kind = (result.failure or {}).get("kind", FailureKind.COMPUTATION_FAILED.value)
        return JSONResponse(status_code=_HTTP_STATUS.get(kind, 502), content=body)

    @app.get("/v1/runs")
    def list_runs(limit: int = Query(50, ge=1, le=200),
                  verdict: str | None = Query(None, pattern="^(allow|deny)$")):
        return {"runs": audit.list_runs(limit=limit, verdict=verdict)}

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str):
        data = audit.get_run(run_id)
        if data is None:
            raise HTTPException(status_code=404, detail={"run_id": run_id, "error": "not_found"})
        return data

    @app.get("/v1/runs/{run_id}/verify")
    def verify_run(run_id: str):
        out = audit.verify_run(run_id)
        if not out["found"]:
            raise HTTPException(status_code=404, detail={"run_id": run_id, "error": "not_found"})
        return out

    @app.get("/v1/key")
    def public_key():
        return {"algorithm": "Ed25519", "public_key_pem": audit.public_key_pem().decode()}

    return app
