"""FastAPI application exposing the model over HTTP.

Every response (success or failure) carries:

* ``request_id``  -- correlation id (client supplied via ``X-Request-Id`` or
  generated), echoed in every structured log line for that request;
* ``version``     -- model version that produced it;
* ``host``        -- processing location (module path) for each key step;
* ``uncertainties`` -- a list that is *separate* from ``failures``. Failures
  are categorical and blocking; uncertainties are non-blocking caveats (for
  example a legacy transaction's fee-cap interpretation). An empty list means
  "no caveats".

The API holds no live-chain connection and performs no forecasting; replay is
an in-memory run over the supplied synthetic payload. Persisted querying of a
named SQLite database is exposed separately under ``/store``.
"""

from __future__ import annotations

import os
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import (BASE_FEE_CHANGE_DENOMINATOR, DEFAULT_GAS_LIMIT,
                      ELASTICITY_MULTIPLIER, INITIAL_BASE_FEE, MAX_U256,
                      TX_TYPE_EIP1559, TX_TYPE_LEGACY)
from ..core.fees import base_fee_step_report, next_base_fee
from ..core.validation import intrinsic_gas, validate_fee_caps
from ..encoding.transaction import Transaction
from ..errors import FailureCode, ModelError
from ..replay.events import EventLog
from ..replay.replay import Replayer, payload_from_dict
from ..storage.store import IndexStore
from .schemas import (NextBaseFeeRequest, ReplayRequest, ValidateFeeRequest)

HOST = "basefee_model.api.main"
DB_PATH = os.environ.get("BASEFEE_DB_PATH", "basefee_model.db")

app = FastAPI(
    title="EIP-1559 Base-Fee Recurrence Model",
    version=__version__,
    description="Synthetic, offline EIP-1559 base-fee + transaction-validity "
                "backend. No live-chain connection, no forecasting.",
)
log = EventLog()


@app.middleware("http")
async def correlate(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex[:12]}"
    request.state.request_id = request_id
    log.info("http_request", component=HOST, request_id=request_id,
             method=request.method, path=request.url.path)
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Model-Version"] = __version__
    return response


def _envelope(request: Request, result: dict | None = None, *,
              failures: list | None = None, uncertainties: list | None = None,
              status: str = "ok", status_code: int = 200,
              steps: list | None = None) -> JSONResponse:
    body = {
        "status": status,
        "request_id": getattr(request.state, "request_id", None),
        "version": __version__,
        "steps": steps or [],
        "result": result,
        "failures": failures or [],
        "uncertainties": uncertainties or [],
    }
    return JSONResponse(body, status_code=status_code)


@app.exception_handler(ModelError)
async def model_error_handler(request: Request, exc: ModelError):
    rid = getattr(request.state, "request_id", None)
    log.error("model_error", code=exc.code.value, message=exc.message,
              component=HOST, request_id=rid, details=exc.details)
    return _envelope(
        request,
        failures=[{
            "code": exc.code.value, "message": exc.message,
            "details": exc.details, "component": "basefee_model.core",
            "request_id": rid,
        }],
        status="error", status_code=422,
    )


@app.get("/health")
async def health(request: Request):
    return _envelope(request, {"healthy": True, "live_chain": False,
                               "forecasting": False})


@app.get("/version")
async def version(request: Request):
    return _envelope(request, {
        "version": __version__,
        "params": {
            "elasticity_multiplier": ELASTICITY_MULTIPLIER,
            "base_fee_change_denominator": BASE_FEE_CHANGE_DENOMINATOR,
            "default_gas_limit": DEFAULT_GAS_LIMIT,
            "initial_base_fee": INITIAL_BASE_FEE,
            "max_uint256": str(MAX_U256),
        },
        "integer_semantics": (
            "floor division on non-negative integers; minimum upward "
            "increment is 1 wei; base fee clamped at >= 0"
        ),
    })


@app.post("/basefee/next")
async def compute_next_base_fee(request: Request, body: NextBaseFeeRequest):
    rid = request.state.request_id
    report = base_fee_step_report(
        body.parent_base_fee, body.parent_gas_used, body.parent_gas_limit)
    log.event("basefee_next", component="basefee_model.core.fees",
              request_id=rid, **report)
    steps = [{
        "host": "basefee_model.core.fees.next_base_fee",
        "description": "pure recurrence from parent parameters only",
        "detail": report,
    }]
    return _envelope(request, report, steps=steps)


def _hex_to_bytes(value: str) -> bytes:
    value = value or "0x"
    return bytes.fromhex(value[2:] if value.startswith("0x") else value)


@app.post("/transactions/validate-fee")
async def validate_transaction_fee(request: Request, body: ValidateFeeRequest):
    rid = request.state.request_id
    uncertainties: list[dict] = []

    if body.tx_type == TX_TYPE_LEGACY:
        if body.gas_price is None:
            raise ModelError("legacy tx requires gas_price",
                             code=FailureCode.INVALID_FIELDS)
        tx = Transaction(
            type=TX_TYPE_LEGACY, nonce=0, gas_limit=body.gas_limit,
            to=b"\x00" * 20, value=body.value, data=_hex_to_bytes(body.data_hex),
            chain_id=1559, gas_price=body.gas_price,
        )
        uncertainties.append({
            "kind": "legacy_fee_semantics",
            "message": ("Legacy tx has a single gas_price treated as both fee "
                        "cap and priority; EIP-1559 tip = gas_price - base_fee."),
        })
    elif body.tx_type == TX_TYPE_EIP1559:
        if body.max_fee_per_gas is None or body.max_priority_fee_per_gas is None:
            raise ModelError(
                "type-2 tx requires max_fee_per_gas and max_priority_fee_per_gas",
                code=FailureCode.INVALID_FIELDS)
        tx = Transaction(
            type=TX_TYPE_EIP1559, nonce=0, gas_limit=body.gas_limit,
            to=b"\x00" * 20, value=body.value, data=_hex_to_bytes(body.data_hex),
            chain_id=1559, max_fee_per_gas=body.max_fee_per_gas,
            max_priority_fee_per_gas=body.max_priority_fee_per_gas,
        )
    else:
        raise ModelError(f"unsupported tx type {body.tx_type}",
                         code=FailureCode.UNSUPPORTED_TX_TYPE)

    ig = intrinsic_gas(tx)
    validate_fee_caps(tx, body.base_fee)
    eff = tx.effective_gas_price(body.base_fee)
    prio = tx.priority_fee_per_gas(body.base_fee)
    result = {
        "intrinsic_gas": ig,
        "gas_limit_ok": body.gas_limit >= ig,
        "effective_gas_price": eff,
        "priority_fee_per_gas": prio,
        "burned_per_gas": eff - prio,
        "upfront_max": (tx.gas_price if tx.type == TX_TYPE_LEGACY
                        else tx.max_fee_per_gas) * tx.gas_limit + tx.value,
        "fee_cap_relation_valid": True,
    }
    log.event("validate_fee", component="basefee_model.core.validation",
              request_id=rid, intrinsic_gas=ig, effective_gas_price=eff)
    steps = [
        {"host": "basefee_model.core.validation.intrinsic_gas",
         "description": "21000 + 4/zero + 16/nonzero calldata byte"},
        {"host": "basefee_model.core.validation.validate_fee_caps",
         "description": "cap>=priority, cap>=base_fee, uint256 overflow"},
    ]
    return _envelope(request, result, steps=steps,
                     uncertainties=uncertainties)


@app.post("/replay")
async def replay(request: Request, body: ReplayRequest,
                 persist: bool = False):
    rid = request.state.request_id
    payloads = [payload_from_dict(b.model_dump()) for b in body.blocks]
    store = IndexStore(DB_PATH if persist else ":memory:")
    try:
        replayer = Replayer(
            store, genesis_base_fee=body.genesis_base_fee,
            gas_limit=body.gas_limit, alloc=body.alloc,
            genesis_gas_used=body.genesis_gas_used, chain_id=body.chain_id,
            log=log)
        report = replayer.run(payloads, request_id=rid)
    finally:
        if not persist:
            store.close()

    status = "ok" if report.ok() else "error"
    code = 200 if report.ok() else 422
    result = {
        "applied": report.applied,
        "skipped": report.skipped,
        "blocks": report.to_dict()["blocks"],
        "conservation": report.conservation,
        "persisted": persist,
    }
    failures = [{**f, "request_id": rid, "component": "basefee_model.replay"}
                for f in report.failures]
    steps = [{
        "host": "basefee_model.replay.replay.Replayer.run",
        "description": ("decode -> recover sender -> validate -> apply -> "
                        "(optional) persist, genesis-derived state"),
        "applied": len(report.applied), "skipped": len(report.skipped),
    }]
    return _envelope(request, result, failures=failures, status=status,
                     status_code=code, steps=steps)


@app.get("/store/totals")
async def store_totals(request: Request):
    if not os.path.exists(DB_PATH):
        raise ModelError("no persisted database yet; run a replay with "
                         "?persist=true", code=FailureCode.EMPTY_CHAIN)
    with IndexStore(DB_PATH) as store:
        return _envelope(request, store.totals())


@app.get("/store/blocks")
async def store_blocks(request: Request):
    if not os.path.exists(DB_PATH):
        raise ModelError("no persisted database yet",
                         code=FailureCode.EMPTY_CHAIN)
    with IndexStore(DB_PATH) as store:
        return _envelope(request, {"blocks": store.base_fee_timeline()})
