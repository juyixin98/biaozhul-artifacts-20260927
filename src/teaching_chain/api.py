"""FastAPI 应用：提交交易 / 只读预演 / 状态查询。

所有应答都带 ``request_id``（客户端可用 ``X-Request-ID`` 指定，
否则服务端生成）；拒绝与异常都会写结构化诊断日志。
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import PROGRAM_VERSION, get_settings
from .diagnostics import DIAGNOSTICS
from .kernel import TransactionError
from .node import LocalNode

REJECT = "REJECT"
ACCEPT = "ACCEPT"
UNDETERMINED = "UNDETERMINED"


class SubmitRequest(BaseModel):
    transactions: list[dict[str, Any]] = Field(min_length=1)


class DryRunRequest(BaseModel):
    transaction: dict[str, Any]


def create_app(db_path: str | Path | None = None, chain: str = "teaching-chain-local") -> FastAPI:
    settings = get_settings(db_path)
    app = FastAPI(
        title="教学链 API",
        version=PROGRAM_VERSION,
        description="确定性小型状态机与 gas 计费的本地教学服务",
    )
    app.state.node = LocalNode(settings.resolved_db_path(), chain=chain)
    app.state.diagnostics = DIAGNOSTICS

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception:
            DIAGNOSTICS.emit(
                "unhandled_exception", request_id,
                decision=UNDETERMINED, reason="未捕获异常",
                path=request.url.path, level=40,  # logging.ERROR
            )
            raise
        response.headers["X-Request-ID"] = request_id
        return response

    def _diag(request: Request) -> Any:
        return request.app.state.diagnostics

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        node = request.app.state.node
        return {"status": "ok", "request_id": request.state.request_id,
                "node": node.status(), "program_version": PROGRAM_VERSION}

    @app.post("/v1/dry-run")
    def dry_run(body: DryRunRequest, request: Request) -> JSONResponse:
        rid = request.state.request_id
        try:
            processed = request.app.state.node.dry_run(body.transaction)
        except TransactionError as exc:
            _diag(request).emit(
                "dry_run_rejected", rid, decision=REJECT, reason=exc.code,
                code=exc.code, chain=(body.transaction or {}).get("chain"),
                tx=body.transaction,
            )
            return JSONResponse(
                status_code=422,
                content={"request_id": rid, "accepted": False,
                         "error_code": exc.code, "detail": str(exc)},
            )
        receipt = processed.receipt
        _diag(request).emit(
            "dry_run_executed", rid,
            decision=ACCEPT if receipt.status == 1 else REJECT,
            reason="执行成功" if receipt.status == 1 else f"执行失败 {receipt.error_category}",
            status=receipt.status, gas_used=receipt.gas_used,
            error_category=receipt.error_category,
            state_root=receipt.state_root, tx=body.transaction,
        )
        return JSONResponse(content={
            "request_id": rid,
            "accepted": True,  # 请求被接受并执行（执行失败见 receipt.status）
            "receipt": receipt.to_dict(),
        })

    @app.post("/v1/blocks")
    def submit_block(body: SubmitRequest, request: Request) -> JSONResponse:
        rid = request.state.request_id
        try:
            result = request.app.state.node.submit(body.transactions)
        except TransactionError as exc:
            _diag(request).emit(
                "submit_rejected", rid, decision=REJECT, reason=exc.code,
                code=exc.code, tx_count=len(body.transactions),
                batch=body.transactions,
            )
            return JSONResponse(
                status_code=422,
                content={"request_id": rid, "accepted": False,
                         "error_code": exc.code, "detail": str(exc)},
            )
        receipts = [p.receipt.to_dict() for p in result.results]
        any_failed = any(p.receipt.status == 0 for p in result.results)
        _diag(request).emit(
            "block_committed", rid,
            decision=ACCEPT if not any_failed else "ACCEPT_WITH_FAILED_TXS",
            reason="整批静态校验通过并封块（含执行失败交易）" if any_failed else "全部执行成功",
            block_number=result.block_number, block_hash=result.block_hash,
            tx_count=len(result.results),
            failed=sum(1 for p in result.results if p.receipt.status == 0),
            batch=body.transactions,
        )
        return JSONResponse(content={
            "request_id": rid,
            "accepted": True,
            "block_number": result.block_number,
            "block_hash": result.block_hash,
            "receipts": receipts,
        })

    @app.get("/v1/status")
    def status(request: Request) -> dict[str, Any]:
        return {"request_id": request.state.request_id,
                "node": request.app.state.node.status(),
                "program_version": PROGRAM_VERSION}

    @app.get("/v1/blocks/{number}")
    def get_block(number: int, request: Request) -> JSONResponse:
        header = request.app.state.node.block(number)
        if header is None:
            return JSONResponse(
                status_code=404,
                content={"request_id": request.state.request_id,
                         "error_code": "BLOCK_NOT_FOUND", "detail": f"区块 {number} 不存在"},
            )
        return JSONResponse(content={"request_id": request.state.request_id, "block": header})

    @app.get("/v1/receipts/{tx_hash}")
    def get_receipt(tx_hash: str, request: Request) -> JSONResponse:
        receipt = request.app.state.node.receipt(tx_hash)
        if receipt is None:
            return JSONResponse(
                status_code=404,
                content={"request_id": request.state.request_id,
                         "error_code": "RECEIPT_NOT_FOUND"},
            )
        return JSONResponse(content={"request_id": request.state.request_id, "receipt": receipt})

    return app


def run(host: str | None = None, port: int | None = None,
        db_path: str | Path | None = None) -> None:
    """进程入口：启动 uvicorn。"""
    import uvicorn

    settings = get_settings(db_path)
    uvicorn.run(
        create_app(settings.resolved_db_path()),
        host=host or settings.host,
        port=port or settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    run()
