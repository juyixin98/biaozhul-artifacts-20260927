"""HTTP API 边界 (FastAPI)。

仅做 JSON 收发、错误信封映射与串行化；所有共识判定都在内核，存储在 SqliteStore。
请求体不经过宽松 pydantic 模型——直接走严格契约解码（utxo_ledger.encoding），
保证未知键/类型错误统一落 INPUT_ERROR/MALFORMED_ENCODING。

错误类别 -> HTTP 状态：
    INPUT_ERROR          400
    STATE_CONFLICT       409
    RESOURCE_EXHAUSTED   422
    COMPUTATION_FAILED   422（签名/根）或 500（存储/内部）

所有提交尝试（成功与失败）都写 RunJournal，返回体含 run_id 便于重放定位。
"""
from __future__ import annotations

import threading
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import encoding
from .errors import (
    ErrorCategory,
    InternalError,
    LedgerError,
    StorageFailureError,
)
from .journal import RunJournal, snapshot
from .kernel import Kernel
from .store import SqliteStore

_HTTP_STATUS = {
    ErrorCategory.INPUT_ERROR: 400,
    ErrorCategory.STATE_CONFLICT: 409,
    ErrorCategory.RESOURCE_EXHAUSTED: 422,
}


def _status_for(exc: LedgerError) -> int:
    if exc.category in _HTTP_STATUS:
        return _HTTP_STATUS[exc.category]
    if isinstance(exc, StorageFailureError):
        return 500
    if isinstance(exc, InternalError):
        return 500
    return 422


def create_app(store: SqliteStore | None = None, *, log_dir: str = "logs") -> FastAPI:
    app = FastAPI(title="UTXO 测试账本", version="1.0.0")
    app.state.store = store or SqliteStore(":memory:")
    app.state.log_dir = log_dir
    app.state.lock = threading.Lock()  # 块提交串行化

    @app.exception_handler(LedgerError)
    async def ledger_error_handler(request: Request, exc: LedgerError) -> JSONResponse:
        return JSONResponse(
            status_code=_status_for(exc),
            content={"ok": False, "error": exc.to_dict()},
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "utxo-ledger", "version": "1.0.0"}

    @app.get("/chain/tip")
    async def chain_tip() -> dict[str, Any]:
        s: SqliteStore = app.state.store
        tip = s.tip()
        return {
            "ok": True,
            "height": None if tip is None else tip[0],
            "block_id": None if tip is None else tip[1].hex(),
            "utxo_count": s.utxo_count(),
            "block_count": s.block_count(),
            "utxo_root": s.utxo_root().hex(),
        }

    @app.get("/utxo/{txid_hex}/{vout}")
    async def get_utxo(txid_hex: str, vout: int) -> JSONResponse:
        s = app.state.store
        try:
            txid = bytes.fromhex(txid_hex)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": {
                        "category": ErrorCategory.INPUT_ERROR.value,
                        "code": "MALFORMED_ENCODING",
                        "message": "txid 不是合法 hex",
                        "details": {"txid": txid_hex},
                    },
                },
            )
        u = s.get_utxo(txid, vout)
        if u is None:
            status = s.classify_outpoint(txid, vout)
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "spent": status == "spent",
                    "exists": False,
                },
            )
        return JSONResponse(
            content={
                "ok": True,
                "exists": True,
                "spent": False,
                "utxo": {
                    "txid": u.txid.hex(),
                    "vout": u.vout,
                    "amount": u.amount,
                    "pubkey": u.pubkey.hex(),
                    "created_height": u.created_height,
                },
            }
        )

    @app.get("/address/{pubkey_hex}/utxos")
    async def address_utxos(pubkey_hex: str) -> JSONResponse:
        s = app.state.store
        try:
            pubkey = bytes.fromhex(pubkey_hex)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": {
                        "category": ErrorCategory.INPUT_ERROR.value,
                        "code": "MALFORMED_ENCODING",
                        "message": "pubkey 不是合法 hex",
                        "details": {"pubkey": pubkey_hex},
                    },
                },
            )
        utxos = s.utxo_by_pubkey(pubkey)
        return JSONResponse(
            content={
                "ok": True,
                "pubkey": pubkey_hex,
                "count": len(utxos),
                "utxos": [
                    {
                        "txid": u.txid.hex(),
                        "vout": u.vout,
                        "amount": u.amount,
                        "created_height": u.created_height,
                    }
                    for u in utxos
                ],
            }
        )

    @app.post("/blocks")
    async def submit_block(request: Request) -> JSONResponse:
        journal = RunJournal(scenario="api_submit_block", log_dir=app.state.log_dir)
        s: SqliteStore = app.state.store
        try:
            raw = await request.json()
        except Exception as exc:
            from .errors import MalformedEncodingError

            err = MalformedEncodingError(f"请求体不是合法 JSON: {exc}")
            journal.failure(err.to_dict())
            journal.finish(
                accepted=False,
                expected_accepted=None,
                match=None,
                before_snapshot=snapshot(s, "before"),
                after_snapshot=snapshot(s, "after"),
                failure=err.to_dict(),
            )
            return JSONResponse(
                status_code=400,
                content={"ok": False, "run_id": journal.run_id, "error": err.to_dict()},
            )

        try:
            block = encoding.block_from_json(raw)
        except LedgerError as exc:
            journal.failure(exc.to_dict())
            journal.finish(
                accepted=False,
                expected_accepted=None,
                match=None,
                before_snapshot=snapshot(s, "before"),
                after_snapshot=snapshot(s, "after_decode_fail"),
                failure=exc.to_dict(),
            )
            return JSONResponse(
                status_code=400,
                content={"ok": False, "run_id": journal.run_id, "error": exc.to_dict()},
            )

        with app.state.lock:
            before = snapshot(s, "before")
            try:
                plan = Kernel(s, event_sink=journal.kernel_event).plan_block(block)
                root_after = s.apply_block(plan)
            except LedgerError as exc:
                after = snapshot(s, "after_reject")
                journal.failure(exc.to_dict())
                journal.finish(
                    accepted=False,
                    expected_accepted=None,
                    match=None,
                    before_snapshot=before,
                    after_snapshot=after,
                    failure=exc.to_dict(),
                )
                preserved = {k: before[k] for k in before if k != "label"} == {
                    k: after[k] for k in after if k != "label"
                }
                return JSONResponse(
                    status_code=_status_for(exc),
                    content={
                        "ok": False,
                        "run_id": journal.run_id,
                        "error": exc.to_dict(),
                        "state_preserved": preserved,
                    },
                )
            except Exception as exc:
                err = InternalError(f"未预期异常: {type(exc).__name__}: {exc}")
                journal.failure(err.to_dict())
                return JSONResponse(
                    status_code=500,
                    content={"ok": False, "run_id": journal.run_id, "error": err.to_dict()},
                )
            after = snapshot(s, "after_accept")
            journal.finish(
                accepted=True,
                expected_accepted=None,
                match=None,
                before_snapshot=before,
                after_snapshot=after,
                extra={"utxo_root_after": root_after.hex()},
            )
            return JSONResponse(
                status_code=201,
                content={
                    "ok": True,
                    "run_id": journal.run_id,
                    "height": plan.height,
                    "block_id": plan.block_id.hex(),
                    "tx_count": len(plan.results),
                    "total_fee": plan.total_fee,
                    "utxo_root_after": root_after.hex(),
                },
            )

    return app
