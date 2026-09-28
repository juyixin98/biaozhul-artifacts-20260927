"""FastAPI application wiring kernel + storage into explainable HTTP endpoints.

Endpoints
---------
POST /v1/fee/next            pure next-base-fee preview (no state change)
POST /v1/fee/effective-tip   effective price/tip preview for one transaction
POST /v1/blocks              validate + execute + persist one block
GET  /v1/blocks/{number}     fetch an accepted block with receipts
GET  /v1/blocks/head         current head / expected parent context
GET  /v1/health              version + readiness

Every response (and error envelope) carries the request id, protocol version
and the component that produced it. Hard failures and non-blocking warnings are
returned in separate arrays.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..params import PARAMS
from ..errors import ErrorCode
from ..kernel import Chain
from ..kernel.execution import ChainState
from ..kernel.models import Block
from ..storage import Store, StorageError
from .logging_setup import StructuredLogger, configure_logging, new_request_id, set_request_id
from .wire import structured_to_transaction, raw_to_transaction, WireError
from .schemas import BlockIn, FeeQuery, FeePreviewTx

DB_PATH = os.environ.get("BASEFEE_DB", ":memory:")


def create_app(db_path: Optional[str] = None) -> FastAPI:
    configure_logging()
    log = StructuredLogger("api")
    app = FastAPI(title="Synthetic EIP-1559 Base-Fee Model", version="1.0.0")
    app.state.store = Store(db_path or DB_PATH)
    # The in-memory chain starts at genesis with empty state. The SQLite store
    # is the durable index; the Chain holds live world state for this process.
    app.state.chain = Chain(ChainState())

    # ---------- middleware: request id + error envelope ----------
    @app.middleware("http")
    async def correlate(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or new_request_id()
        set_request_id(rid)
        try:
            response = await call_next(request)
        except Exception as exc:  # pragma: no cover - defensive
            log.failure("unhandled_exception", str(exc), error=type(exc).__name__)
            return _envelope(500, rid, failures=[{"code": "E999_INTERNAL",
                                                  "detail": str(exc)}])
        response.headers["X-Request-ID"] = rid
        response.headers["X-Protocol-Version"] = PARAMS.protocol_version
        return response

    @app.exception_handler(WireError)
    async def wire_error_handler(request: Request, exc: WireError):
        log.failure("wire_decode_failed", exc.detail, code=exc.code)
        return _envelope(422, _rid(request), failures=[{"code": exc.code,
                                                        "detail": exc.detail}])

    # ---------- health ----------
    @app.get("/v1/health")
    async def health():
        return {
            "request_id": _current_rid(),
            "component": "api",
            "protocol_version": PARAMS.protocol_version,
            "fixed_parameters": {
                "elasticity_multiplier": PARAMS.elasticity_multiplier,
                "base_fee_max_change_denominator":
                    PARAMS.base_fee_max_change_denominator,
                "intrinsic_tx_gas": PARAMS.intrinsic_tx_gas,
                "genesis_base_fee": str(PARAMS.genesis_base_fee),
            },
            "head_number": app.state.chain.head.number,
            "status": "ok",
            "boundaries": [
                "synthetic offline model; no real chain connection",
                "no economic forecasting",
            ],
        }

    # ---------- pure fee previews ----------
    @app.post("/v1/fee/next")
    async def fee_next(body: FeeQuery):
        from ..kernel.eip1559 import compute_next_base_fee_step
        base_fee = int(body.parent_base_fee, 10)
        log.step("fee_preview", "computing next base fee",
                 parent_base_fee=base_fee, gas_used=body.gas_used,
                 gas_limit=body.gas_limit)
        try:
            step = compute_next_base_fee_step(base_fee, body.gas_used, body.gas_limit)
        except ValueError as exc:
            code = (ErrorCode.E040_BLOCK_GAS_EXCEEDED.value
                    if "gas_used" in str(exc) and "exceeds" in str(exc)
                    else ErrorCode.E045_GAS_LIMIT_INVALID.value)
            log.failure("fee_preview_rejected", str(exc), code=code)
            return _envelope(422, _current_rid(),
                             failures=[{"code": code, "detail": str(exc)}])
        return {
            "request_id": _current_rid(),
            "component": "kernel.eip1559",
            "protocol_version": PARAMS.protocol_version,
            "result": _step_out(step),
            "failures": [],
            "warnings": [],
        }

    @app.post("/v1/fee/effective-tip")
    async def effective_tip(body: FeePreviewTx):
        from ..kernel.eip1559 import effective_priority_tip, effective_gas_price
        base_fee = int(body.base_fee, 10)
        max_fee = int(body.max_fee_per_gas, 10)
        max_tip = int(body.max_priority_fee_per_gas, 10)
        warnings: list[dict] = []
        if max_fee < base_fee:
            return _envelope(422, _current_rid(), failures=[{
                "code": ErrorCode.E020_MAX_FEE_BELOW_BASE.value,
                "detail": f"max_fee {max_fee} < base_fee {base_fee}"}])
        if max_tip > max_fee:
            # Legal per EIP-1559, but a likely client mistake: surface as warning.
            warnings.append({
                "code": "W001_PRIORITY_CAP_ABOVE_FEE_CAP",
                "detail": "max_priority_fee exceeds max_fee; effective tip is "
                          "capped at max_fee - base_fee",
            })
        tip = effective_priority_tip(base_fee=base_fee, max_fee_per_gas=max_fee,
                                     max_priority_fee_per_gas=max_tip)
        price = effective_gas_price(base_fee=base_fee, max_fee_per_gas=max_fee,
                                    max_priority_fee_per_gas=max_tip)
        return {
            "request_id": _current_rid(),
            "component": "kernel.eip1559",
            "protocol_version": PARAMS.protocol_version,
            "result": {
                "base_fee": str(base_fee),
                "effective_priority_tip": str(tip),
                "effective_gas_price": str(price),
                "burned_at_gas_limit": str(base_fee * body.gas_limit),
                "tip_at_gas_limit": str(tip * body.gas_limit),
            },
            "failures": [],
            "warnings": warnings,
        }

    # ---------- blocks ----------
    @app.post("/v1/blocks", status_code=201)
    async def submit_block(body: BlockIn):
        ctx = app.state.chain.parent_context()
        warnings: list[dict] = []

        txs = []
        for i, obj in enumerate(body.transactions):
            try:
                txs.append(structured_to_transaction(obj))
            except WireError as exc:
                log.failure("tx_decode_failed", f"structured tx[{i}]", code=exc.code)
                return _envelope(422, _current_rid(), failures=[{
                    "code": exc.code, "detail": f"transactions[{i}]: {exc.detail}"}])
        for i, raw in enumerate(body.raw_transactions):
            try:
                txs.append(raw_to_transaction(raw))
            except WireError as exc:
                log.failure("tx_decode_failed", f"raw tx[{i}]", code=exc.code)
                return _envelope(422, _current_rid(), failures=[{
                    "code": exc.code, "detail": f"raw_transactions[{i}]: {exc.detail}"}])

        if body.transactions and body.raw_transactions:
            warnings.append({
                "code": "W002_MIXED_TX_ENCODING",
                "detail": "structured transactions are ordered before raw ones",
            })

        block = Block(
            number=body.number,
            parent_hash=body.parent_hash,
            base_fee_per_gas=int(body.base_fee_per_gas, 10),
            gas_limit=body.gas_limit,
            gas_used=body.gas_used,
            transactions=txs,
        )
        log.step("block_submitted", "executing block", number=block.number,
                 tx_count=len(txs), strict=body.strict,
                 expected_parent=ctx["parent_number"])
        executed = app.state.chain.apply_block(block, strict=body.strict)

        if not executed.accepted:
            log.failure("block_rejected", executed.block_error_detail or "",
                        code=executed.block_error, number=block.number)
            return _envelope(422, _current_rid(), failures=[{
                "code": executed.block_error,
                "detail": executed.block_error_detail,
            }], warnings=warnings, result={
                "number": block.number,
                "expected_parent": ctx,
            })

        try:
            app.state.store.save_executed(executed)
        except StorageError as exc:
            log.failure("block_persist_failed", exc.detail, code=exc.code)
            return _envelope(409, _current_rid(), failures=[{
                "code": exc.code, "detail": exc.detail}])

        log.step("block_accepted", "block executed and indexed",
                 number=block.number, block_hash=block.block_hash,
                 next_base_fee=executed.next_base_fee,
                 invalid_count=len(executed.invalid))
        return {
            "request_id": _current_rid(),
            "component": "kernel+storage",
            "protocol_version": PARAMS.protocol_version,
            "result": {
                "number": block.number,
                "block_hash": block.block_hash,
                "next_base_fee": str(executed.next_base_fee),
                "fee_step": _step_out_dict(executed.fee_step_explanation),
                "receipts": [
                    {
                        "tx_index": i,
                        "tx_hash": r.tx_hash,
                        "sender": r.sender,
                        "valid": r.valid,
                        "error_code": r.error_code,
                        "gas": str(r.gas) if r.valid else None,
                        "effective_priority_tip":
                            str(r.effective_priority_tip) if r.valid else None,
                        "effective_gas_price":
                            str(r.effective_gas_price) if r.valid else None,
                        "burned": str(r.burned) if r.valid else None,
                        "tip": str(r.tip) if r.valid else None,
                        "total_cost": str(r.total_cost) if r.valid else None,
                    }
                    for i, r in enumerate(executed.receipts)
                ],
                "skipped_invalid": [
                    {"tx_index": rec.index, "tx_hash": rec.tx_hash,
                     "error_code": rec.error_code, "detail": rec.detail}
                    for rec in executed.invalid
                ],
                "conservation": {
                    "total_burned": str(executed.total_burned),
                    "total_tipped": str(executed.total_tipped),
                    "burn_address": executed.burned_address,
                },
            },
            "failures": [],
            "warnings": warnings,
        }

    @app.get("/v1/blocks/head")
    async def head():
        h = app.state.chain.head
        return {
            "request_id": _current_rid(),
            "component": "kernel.chain",
            "protocol_version": PARAMS.protocol_version,
            "result": {
                "number": h.number,
                "block_hash": h.block_hash,
                "base_fee": str(h.base_fee),
                "gas_limit": h.gas_limit,
                "gas_used": h.gas_used,
                "next_base_fee": str(h.next_base_fee),
                "expected_parent_for_next": app.state.chain.parent_context(),
            },
        }

    @app.get("/v1/blocks/{number}")
    async def get_block(number: int):
        row = app.state.store.get_block_row(number)
        if row is None:
            return _envelope(404, _current_rid(), failures=[{
                "code": "E404_NOT_FOUND",
                "detail": f"block {number} not in index"}])
        txs = app.state.store.get_transactions(number)
        invalid = app.state.store.get_invalid(number)
        import json as _json
        return {
            "request_id": _current_rid(),
            "component": "storage",
            "protocol_version": PARAMS.protocol_version,
            "result": {
                "number": row["number"],
                "block_hash": row["block_hash"],
                "parent_hash": row["parent_hash"],
                "base_fee_per_gas": row["base_fee"],
                "gas_limit": int(row["gas_limit"]),
                "gas_used": int(row["gas_used"]),
                "next_base_fee": row["next_base_fee"],
                "fee_step": _json.loads(row["fee_step_json"]),
                "transactions": [
                    {
                        "tx_index": t["tx_index"],
                        "tx_hash": t["tx_hash"],
                        "sender": t["sender"],
                        "valid": bool(t["valid"]),
                        "error_code": t["error_code"],
                        "gas": t["gas"],
                        "tip": t["tip"],
                        "effective_gas_price": t["gas_price"],
                        "burned": t["burned"],
                        "total_cost": t["total_cost"],
                    }
                    for t in txs
                ],
                "skipped_invalid": [
                    {"tx_index": t["tx_index"], "tx_hash": t["tx_hash"],
                     "error_code": t["error_code"], "detail": t["detail"]}
                    for t in invalid
                ],
            },
        }

    return app


def _step_out(step):
    return {
        "parent_base_fee": str(step.parent_base_fee),
        "gas_limit": step.gas_limit,
        "gas_used": step.gas_used,
        "target_gas": step.target_gas,
        "direction": step.direction,
        "delta_numerator": str(step.delta_numerator),
        "delta_after_first_floor": str(step.delta_after_first_floor),
        "delta_final": str(step.delta_final),
        "applied_min_increment": step.applied_min_increment,
        "next_base_fee": str(step.next_base_fee),
        "denominator": PARAMS.base_fee_max_change_denominator,
        "elasticity_multiplier": PARAMS.elasticity_multiplier,
    }


def _step_out_dict(d: dict) -> dict:
    out = dict(d)
    for key in ("parent_base_fee", "delta_numerator", "delta_after_first_floor",
                "delta_final", "next_base_fee"):
        if key in out:
            out[key] = str(out[key])
    return out


def _rid(request: Request) -> str:
    return request.headers.get("X-Request-ID", "-")


def _current_rid() -> str:
    from .logging_setup import get_request_id
    return get_request_id()


def _envelope(status: int, request_id: str, *, failures=None, warnings=None,
              result=None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "request_id": request_id,
            "component": "api",
            "protocol_version": PARAMS.protocol_version,
            "ok": False,
            "result": result,
            "failures": failures or [],
            "warnings": warnings or [],
        },
    )


app = create_app()
