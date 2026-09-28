"""FastAPI 应用：把 HTTP 适配到 ``Kernel``，自身不含池策略。

可观测性约定：
* 每个请求都有 request_id（``X-Request-Id`` 或自动生成），响应体回显，
  并可在 ``GET /audit/requests/{id}`` 拉到完整处理轨迹；
* 失败响应的 ``error`` 是稳定错误码，``details`` 给出复核所需数字；
* ``uncertainties`` 与硬失败分开，专放"操作成功但存在不确定结论"的条目。
"""

from __future__ import annotations

import logging
from typing import Any

from eth_utils import decode_hex
from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..core.config import Config
from ..core.crypto import decode_signed_transaction
from ..core.kernel import Kernel, OperationResult, new_request_id
from ..core.models import ErrorCode, TxError, TxStatus
from ..storage.repository import IntegrityError
from .logging_setup import (
    configure_logging,
    reset_request_id,
    set_request_id,
)
from .schemas import (
    AccountCreateRequest,
    RawTxSubmitRequest,
    RawTxsBlockRequest,
    RollbackRequest,
)

# ErrorCode -> HTTP 状态码。所有业务失败都走 4xx 并带稳定错误码。
_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_SIGNATURE: 400,
    ErrorCode.WRONG_CHAIN_ID: 400,
    ErrorCode.MALFORMED_TRANSACTION: 400,
    ErrorCode.INTRINSIC_GAS_TOO_LOW: 400,
    ErrorCode.GAS_LIMIT_EXCEEDS_BLOCK: 400,
    ErrorCode.DATA_TOO_LARGE: 413,
    ErrorCode.GAS_PRICE_BELOW_MINIMUM: 400,
    ErrorCode.NONCE_TOO_LOW: 409,
    ErrorCode.NONCE_TOO_FAR_AHEAD: 422,
    ErrorCode.SENDER_SLOT_LIMIT: 429,
    ErrorCode.INSUFFICIENT_FUNDS: 422,
    ErrorCode.SAME_NONCE_LOWER_PRICE: 409,
    ErrorCode.SAME_TRANSACTION_KNOWN: 409,
    ErrorCode.POOL_FULL: 429,
    ErrorCode.TX_NOT_FOUND: 404,
    ErrorCode.BLOCK_FULL: 409,
    ErrorCode.BLOCK_ROLLBACK_FINALIZED: 409,
    ErrorCode.BLOCK_NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
}


def create_app(kernel: Kernel, config: Config) -> FastAPI:
    logger = configure_logging(config.log_level)
    app = FastAPI(
        title="local-txpool",
        version=__version__,
        description=(
            "本地合成账户交易池：按发送者 nonce、费用与容量管理待执行交易。"
            "所有数据为合成夹具，不连接任何生产链。"
        ),
    )
    app.state.kernel = kernel
    app.state.config = config
    app.state.logger = logger

    # ---------------------- 中间件：请求身份 ---------------------- #
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        incoming = request.headers.get("X-Request-Id")
        request_id = incoming or new_request_id("http")
        token = set_request_id(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            reset_request_id(token)
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Service-Version"] = __version__
        return response

    # ---------------------- 异常 -> 稳定错误体 ---------------------- #
    @app.exception_handler(TxError)
    async def tx_error_handler(request: Request, exc: TxError):
        rid = getattr(request.state, "request_id", "-")
        logger.warning("操作失败 code=%s: %s", exc.code.value, exc.message)
        status = _HTTP_STATUS.get(exc.code, 400)
        return JSONResponse(
            status_code=status,
            content=_error_body(exc.code, exc.message, exc.details, rid),
        )

    @app.exception_handler(IntegrityError)
    async def integrity_handler(request: Request, exc: IntegrityError):
        rid = getattr(request.state, "request_id", "-")
        logger.error("存储不变量被破坏: %s", exc)
        return JSONResponse(
            status_code=500,
            content=_error_body(
                ErrorCode.CONFLICT,
                "内部索引不变量被破坏（已回滚该请求）",
                {"integrity": str(exc)},
                rid,
            ),
        )

    def _error_body(
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None,
        request_id: str,
        uncertainties: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "error": code.value,
            "message": message,
            "request_id": request_id,
            "service_version": __version__,
            "details": details or {},
            "uncertainties": uncertainties or [],
            "audit_query": f"/audit/requests/{request_id}",
        }

    def _result_body(
        request_id: str,
        result: OperationResult,
        *,
        extra: dict[str, Any] | None = None,
        uncertainties: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "accepted": result.accepted,
            "request_id": request_id,
            "service_version": __version__,
            "tx_hash": result.tx_hash,
            "status": result.status.value if result.status else None,
            "reason": result.reason,
            "audit_ids": result.audit_ids,
            "audit_query": f"/audit/requests/{request_id}",
            "uncertainties": uncertainties or [],
        }
        if result.error_code is not None:
            body["error"] = result.error_code.value
            body["details"] = result.detail
        if extra:
            body.update(extra)
        return body

    # ---------------------- 系统 ---------------------- #
    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "service_version": __version__,
            "chain_id": config.chain.chain_id,
            "time_ms": kernel.now_ms(),
        }

    @app.get("/chain/head")
    async def chain_head():
        return kernel.chain_head()

    # ---------------------- 账户（合成夹具） ---------------------- #
    @app.post("/accounts", status_code=201)
    async def create_account(payload: AccountCreateRequest, request: Request):
        rid = request.state.request_id
        if payload.credit:
            acct = kernel.create_or_fund_account(
                payload.address, payload.balance, request_id=rid
            )
        else:
            with kernel.repo.transaction() as conn:
                acct = kernel.repo.upsert_account(
                    conn, payload.address, payload.balance
                )
        return {
            "address": acct.address,
            "balance": acct.balance,
            "nonce": acct.nonce,
            "projected_balance": acct.projected_balance,
            "request_id": rid,
        }

    @app.get("/accounts/{address}")
    async def get_account(address: str, request: Request):
        acct = kernel.get_account(address)
        if acct is None:
            raise TxError(
                ErrorCode.MALFORMED_TRANSACTION,
                f"账户不存在: {address}",
                {"address": address},
            )
        return {
            "address": acct.address,
            "balance": acct.balance,
            "nonce": acct.nonce,
            "projected_balance": acct.projected_balance,
        }

    # ---------------------- 交易 ---------------------- #
    @app.post("/transactions", status_code=202)
    async def submit_transaction(
        payload: RawTxSubmitRequest, request: Request
    ):
        rid = request.state.request_id
        try:
            tx = decode_signed_transaction(
                decode_hex(payload.raw_tx),
                expected_chain_id=config.chain.chain_id,
            )
        except TxError as exc:
            logger.info("验签失败 code=%s", exc.code.value)
            return JSONResponse(
                status_code=_HTTP_STATUS.get(exc.code, 400),
                content=_error_body(
                    exc.code, exc.message, exc.details, rid
                ),
            )
        result = kernel.submit_transaction(tx, request_id=rid)
        status = 202 if result.accepted else _HTTP_STATUS.get(
            result.error_code or ErrorCode.CONFLICT, 400
        )
        return JSONResponse(
            status_code=status, content=_result_body(rid, result)
        )

    @app.get("/transactions/pool")
    async def get_pool():
        return kernel.list_pool()

    @app.get("/transactions/{tx_hash}")
    async def get_transaction(tx_hash: str):
        stored = kernel.get_tx(tx_hash)
        if stored is None:
            raise TxError(
                ErrorCode.TX_NOT_FOUND,
                f"交易不存在: {tx_hash}",
                {"tx_hash": tx_hash},
            )
        return kernel._stored_dict(stored)  # noqa: SLF001

    @app.get("/candidate")
    async def candidate():
        return kernel.preview_candidate()

    # ---------------------- 过期/淘汰（管理动作） ---------------------- #
    @app.post("/pool/expire")
    async def expire(request: Request):
        rid = request.state.request_id
        expired = kernel.expire_pending(request_id=rid)
        return {
            "expired": expired,
            "count": len(expired),
            "request_id": rid,
            "audit_query": f"/audit/requests/{rid}",
        }

    # ---------------------- 区块 ---------------------- #
    @app.post("/blocks/propose", status_code=201)
    async def propose_block(
        request: Request,
        payload: RawTxsBlockRequest | None = Body(default=None),
    ):
        rid = request.state.request_id
        payload = payload or RawTxsBlockRequest()
        external = []
        parse_failures: list[dict[str, Any]] = []
        for raw in payload.external_raw_txs:
            try:
                external.append(
                    decode_signed_transaction(
                        decode_hex(raw),
                        expected_chain_id=config.chain.chain_id,
                    )
                )
            except TxError as exc:
                parse_failures.append(
                    {
                        "raw_prefix": raw[:20],
                        "error": exc.code.value,
                        "message": exc.message,
                    }
                )

        block, plan, rejected = kernel.propose_block(
            request_id=rid, external_txs=external
        )
        return {
            "request_id": rid,
            "service_version": __version__,
            "block_hash": block.block_hash,
            "number": block.number,
            "parent_hash": block.parent_hash,
            "gas_used": block.gas_used,
            "ordered": [
                kernel._stored_dict(s) for s in plan.ordered  # noqa: SLF001
            ],
            "applied": list(block.executed_tx_hashes),
            "skipped": [
                {"tx_hash": sk.tx_hash, "reason": sk.reason, **sk.detail}
                for sk in plan.skipped
            ],
            "external_rejected": [
                {
                    "tx_hash": tx.tx_hash,
                    "error": err.code.value,
                    "message": err.message,
                    "details": err.details,
                }
                for tx, err in rejected
            ],
            "external_malformed": parse_failures,
            "uncertainties": [
                {"kind": "block_unconfirmed",
                 "message": "区块尚未达到确认深度，可被回滚",
                 "confirmation_depth": config.finality.confirmation_depth}
            ],
            "audit_query": f"/audit/requests/{rid}",
        }

    @app.post("/blocks/confirm")
    async def confirm_depth(request: Request):
        rid = request.state.request_id
        confirmed = kernel.confirm_depth(request_id=rid)
        return {"confirmed_blocks": confirmed, "count": len(confirmed),
                "request_id": rid}

    @app.post("/blocks/rollback")
    async def rollback(payload: RollbackRequest, request: Request):
        rid = request.state.request_id
        result = kernel.rollback_to(
            payload.target_number, request_id=rid
        )
        uncertainties: list[dict[str, Any]] = []
        for item in result.get("readmit_failed", []):
            uncertainties.append(
                {"kind": "readmit_failed_after_rollback", **item}
            )
        return {"request_id": rid, **result,
                "uncertainties": uncertainties,
                "audit_query": f"/audit/requests/{rid}"}

    # ---------------------- 审计 ---------------------- #
    @app.get("/audit/events")
    async def audit_events(after_id: int = 0, limit: int = 100):
        return {"events": kernel.audit_events(after_id=after_id, limit=limit)}

    @app.get("/audit/requests/{request_id}")
    async def audit_for_request(request_id: str):
        return kernel.audit_for_request(request_id)

    return app
