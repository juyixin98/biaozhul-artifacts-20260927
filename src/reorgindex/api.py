"""FastAPI 接口层。

- 每个请求有 request_id（入站头 X-Request-ID 或自动生成），写入与拒绝诊断都带上；
- 领域错误映射为稳定的错误类别字符串，HTTP 状态仅作辅助，测试按 category 断言；
- 只读端点只反映某个已提交链版本的派生结果（内核切换是事务化的）。
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .diagnostics import JsonDiagnostics
from .errors import (
    DecodeError,
    DuplicateBlockError,
    FinalityReorgError,
    IndexError as DomainError,
    UnknownParentError,
    VerificationError,
)
from .kernel import ChainKernel
from .models import Block
from .replay import verify_against_rebuild
from .storage import Storage

# category -> HTTP 状态。unknown_parent 不是拒绝（202 已挂起）。
_ERROR_STATUS = {
    "decode_error": 400,
    "verification_error": 422,
    "consensus_rule": 422,
    "duplicate_block": 409,
    "finality_reorg": 409,
    "unknown_parent": 202,
}


def create_app(settings: Settings | None = None, storage: Storage | None = None,
               diagnostics: JsonDiagnostics | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    owns_storage = storage is None
    storage = storage or Storage(settings.db_path)
    diagnostics = diagnostics or JsonDiagnostics(level=settings.log_level, redact_keep=settings.redact_keep)
    kernel = ChainKernel(storage, settings, diagnostics)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if owns_storage:
            storage.close()

    app = FastAPI(title="reorgindex", version="0.1.0", lifespan=lifespan)
    app.state.kernel = kernel
    app.state.storage = storage
    app.state.settings = settings
    app.state.diagnostics = diagnostics

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or f"req-{uuid.uuid4().hex[:12]}"
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    def domain_error_response(exc: DomainError, request: Request) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        status = _ERROR_STATUS[exc.category]
        # unknown_parent 在 ingest 路径不会以异常抛出，这里仅作防御性映射。
        return JSONResponse(status_code=status, content={
            "ok": False, "request_id": request_id, "error": exc.to_dict(),
        })

    @app.get("/health")
    def health():
        return {"ok": True, "service": "reorgindex"}

    @app.get("/chain/state")
    def chain_state():
        return {"ok": True, **kernel.state_summary()}

    @app.get("/chain/blocks/{block_hash}")
    def get_block(block_hash: str):
        raw = storage.get_block_raw(block_hash)
        if raw is None:
            return JSONResponse(status_code=404, content={
                "ok": False, "error": {"category": "not_found", "message": "区块未知",
                                       "context": {"block_hash": block_hash}}})
        return {"ok": True, "block": raw}

    @app.get("/chain/blocks/{block_hash}/confirmations")
    def confirmations(block_hash: str):
        if storage.get_block(block_hash) is None:
            return JSONResponse(status_code=404, content={
                "ok": False, "error": {"category": "not_found", "message": "区块未知",
                                       "context": {"block_hash": block_hash}}})
        depth = kernel.confirmation_depth(block_hash)
        return {"ok": True, "block_hash": block_hash,
                "confirmation_depth": depth,
                "on_canonical": depth is not None,
                "finalized": kernel.is_finalized(block_hash),
                "finality_depth": settings.finality_depth,
                "finalized_height": kernel.finalized_height()}

    @app.get("/balances/{address}")
    def balance(address: str):
        return {"ok": True, "address": address, "balance": kernel.get_balance(address)}

    @app.get("/balances")
    def balances():
        return {"ok": True, "balances": kernel.all_balances(),
                "contribution_count": storage.contribution_count()}

    @app.post("/blocks")
    async def submit_block(request: Request):
        request_id = getattr(request.state, "request_id", None)
        try:
            payload = await request.json()
            Block.from_dict(payload)  # 边界形状校验，给出 422 而非 500
        except Exception as exc:
            diagnostics.emit("block_rejected", request_id=request_id, level=30,
                             reason="decode_error", detail=f"请求体不是合法区块: {exc}")
            return JSONResponse(status_code=400, content={
                "ok": False, "request_id": request_id,
                "error": {"category": "decode_error",
                          "message": f"请求体不是合法区块: {exc}", "context": {}}})
        try:
            outcome = kernel.ingest(payload, request_id=request_id)
        except DomainError as exc:
            return domain_error_response(exc, request)
        return JSONResponse(status_code=202 if outcome.pending else 200,
                            content={"ok": True, "request_id": request_id,
                                     "outcome": outcome.model_dump()})

    @app.get("/diagnostics")
    def get_diagnostics(limit: int = 30):
        return {"ok": True, "events": storage.recent_diag(limit)}

    @app.get("/reorgs")
    def get_reorgs(limit: int = 20):
        return {"ok": True, "reorgs": storage.recent_reorgs(limit)}

    @app.post("/debug/rebuild-check")
    def rebuild_check():
        result = verify_against_rebuild(kernel)
        return JSONResponse(status_code=200 if result["ok"] else 409,
                            content={"ok": result["ok"], "mismatches": result["mismatches"],
                                     "current": result["current"], "rebuilt": result["rebuilt"]})

    return app


def run() -> None:  # pragma: no cover - 进程入口
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host="127.0.0.1", port=8000)
