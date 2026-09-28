"""FastAPI 适配层：HTTP 只做 I/O，执行语义全部在内核。

接口
----
* ``GET  /health``                 —— 健康检查与引擎版本
* ``POST /tx``                     —— 提交签名交易信封（接受即返回收据）
* ``GET  /receipts/{height}``      —— 按高度取收据
* ``GET  /tx/{tx_hash}``           —— 按交易摘要取收据
* ``GET  /receipts``               —— 最近收据列表
* ``GET  /accounts/{address}``     —— nonce / 余额索引视图
* ``GET  /storage/{address}/{slot}`` —— 存储槽索引视图
* ``POST /admin/replay``           —— 离线重放校验（不改动数据）

请求标识由中间件注入：优先读 ``X-Request-ID``，否则由宿主层（此处允许 uuid，
仅用于日志关联）生成；确定性内核本身不收到它、也不依赖它。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import get_settings
from .diagnostics import Diagnostics
from .errors import Rejected, ReplayMismatch
from .replay import replay_store
from .service import ChainService
from .storage import IndexStore
from .version import ENGINE_VERSION


class ErrorBody(BaseModel):
    error: str
    code: str
    request_id: str


def create_app(db_path: str | None = None, *, seed_fixtures: bool = True,
               init_credit: int | None = None) -> FastAPI:
    settings = get_settings()
    diag = Diagnostics()
    store = IndexStore(db_path or settings.db_path)
    service = ChainService(store, diag=diag)

    if seed_fixtures:
        from . import fixtures
        for name in ("alice", "bob"):
            addr = fixtures.address_of(name)
            if service.state.balances.get(addr, 0) == 0 and store.get_account(addr) is None:
                service.seed(addr, init_credit or settings.init_credit)

    app = FastAPI(title="teachchain", version=ENGINE_VERSION)
    app.state.store = store
    app.state.service = service
    app.state.diag = diag

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    def _rid(request: Request) -> str:
        return getattr(request.state, "request_id", "-")

    @app.exception_handler(Rejected)
    async def rejected_handler(request: Request, exc: Rejected):
        rid = _rid(request)
        diag("warning", f"http_rejected:{exc.code}",
             {"request_id": rid, "reason": exc.message})
        return JSONResponse(
            status_code=422,
            content={"error": exc.message, "code": exc.code,
                     "request_id": rid},
        )

    @app.exception_handler(ReplayMismatch)
    async def mismatch_handler(request: Request, exc: ReplayMismatch):
        rid = _rid(request)
        diag("error", "replay_mismatch",
             {"request_id": rid, "height": exc.height, "tx": exc.tx_hash[:16],
              "field": exc.field})
        return JSONResponse(
            status_code=409,
            content={"error": str(exc), "code": "replay_mismatch",
                     "request_id": rid,
                     "detail": {"height": exc.height, "field": exc.field,
                                "stored": exc.stored, "computed": exc.computed}},
        )

    @app.get("/health")
    async def health():
        return {"status": "ok", "engine_version": ENGINE_VERSION,
                "height": store.max_height()}

    @app.post("/tx")
    async def submit_tx(request: Request) -> dict[str, Any]:
        envelope = await request.json()
        return service.submit(envelope)

    @app.get("/receipts/{height}")
    async def receipt_by_height(request: Request, height: int):
        r = service.get_receipt(height=height)
        if r is None:
            return JSONResponse(status_code=404,
                                content={"error": "not found", "code": "not_found",
                                         "request_id": _rid(request)})
        return r

    @app.get("/tx/{tx_hash}")
    async def receipt_by_tx(request: Request, tx_hash: str):
        r = service.get_receipt(tx_hash=tx_hash)
        if r is None:
            return JSONResponse(status_code=404,
                                content={"error": "not found", "code": "not_found",
                                         "request_id": _rid(request)})
        return r

    @app.get("/receipts")
    async def list_receipts(limit: int = 50):
        return {"items": store.list_receipts(limit=min(max(limit, 1), 500))}

    @app.get("/accounts/{address}")
    async def account(request: Request, address: str):
        acct = store.get_account(address.lower())
        if acct is None:
            return JSONResponse(status_code=404,
                                content={"error": "unknown account",
                                         "code": "not_found",
                                         "request_id": _rid(request)})
        return {"address": address.lower(), **acct}

    @app.get("/storage/{address}/{slot}")
    async def slot(request: Request, address: str, slot: int):
        value = store.get_slot(address.lower(), slot)
        if value is None:
            return JSONResponse(status_code=404,
                                content={"error": "slot unset", "code": "not_found",
                                         "request_id": _rid(request)})
        return {"address": address.lower(), "slot": slot, "value": value}

    @app.post("/admin/replay")
    async def admin_replay():
        report = replay_store(store, stop_on_first=False)
        code = 200 if report.summary()["ok"] else 409
        return JSONResponse(status_code=code, content=report.summary())

    return app


app = create_app()
