"""FastAPI HTTP boundary.

Thin transport: hex/JSON in, JSON out. Every chain decision is made by
:class:`~utxo_ledger.node.LedgerNode`; this module never validates chain rules
itself. Error status mapping mirrors the four error categories:

* ``input``       -> 400 Bad Request
* ``state``       -> 409 Conflict
* ``resource``    -> 422 Unprocessable Entity (size/count limits)
* ``computation`` -> 500 Internal Server Error
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import ErrorCategory, ErrorCode, LedgerError
from .node import LedgerNode
from .runlog import RunLogger
from .storage import SqliteStore

_STATUS = {
    ErrorCategory.INPUT: 400,
    ErrorCategory.STATE: 409,
    ErrorCategory.RESOURCE: 422,
    ErrorCategory.COMPUTATION: 500,
}


class BlockSubmission(BaseModel):
    raw_hex: str = Field(..., description="canonically encoded block, hex")


class BlockAccepted(BaseModel):
    accepted: bool
    height: int | None
    block_hash: str | None
    fee_total: int | None
    state_unchanged: bool


def create_app(
    db_path: str = ":memory:",
    logdir: str | None = None,
    run_id: str | None = None,
) -> FastAPI:
    app = FastAPI(title="Test-asset UTXO ledger", version="0.1.0", docs_url="/docs")
    # A single shared connection; writes are serialized by BEGIN IMMEDIATE.
    store = SqliteStore(db_path)
    logger = RunLogger(logdir, run_id=run_id) if logdir else None
    if logger is not None:
        logger.run_start("api", db_path=db_path)
    node = LedgerNode(store, logger)
    app.state.store = store
    app.state.logger = logger
    app.state.seq = 0

    @app.exception_handler(LedgerError)
    async def _ledger_exc_handler(_request, exc: LedgerError):
        return JSONResponse(
            status_code=_STATUS.get(exc.category, 500),
            content={"error": exc.to_dict()},
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "tip": node.tip}

    @app.get("/chain/tip")
    async def tip() -> dict[str, Any]:
        return node.tip

    @app.get("/chain/blocks/{height}")
    async def block(height: int) -> dict[str, Any]:
        info = store.get_block_info(height)
        if info is None:
            raise LedgerError(ErrorCode.NOT_FOUND, f"no block at height {height}")
        return info

    @app.get("/chain/txs/{txid_hex}")
    async def tx(txid_hex: str) -> dict[str, Any]:
        txid = _parse_hex(txid_hex, 32, "txid", ErrorCode.BAD_TXID)
        info = store.get_tx_info(txid)
        if info is None:
            raise LedgerError(ErrorCode.NOT_FOUND, f"unknown txid {txid_hex}")
        return info

    @app.get("/utxos/{txid_hex}/{vout}")
    async def utxo(txid_hex: str, vout: int) -> dict[str, Any]:
        txid = _parse_hex(txid_hex, 32, "txid", ErrorCode.BAD_TXID)
        info = store.get_utxo(txid, vout)
        if info is None:
            raise LedgerError(ErrorCode.NOT_FOUND, "utxo not found or spent")
        return info

    @app.get("/utxos")
    async def list_utxos(
        pubkey_hex: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict[str, Any]:
        pubkey = None
        if pubkey_hex is not None:
            pubkey = _parse_hex(
                pubkey_hex, 32, "pubkey", ErrorCode.BAD_PUBLIC_KEY
            )
        items = store.list_utxos(pubkey, limit=limit)
        return {"count": len(items), "utxos": items}

    @app.post("/blocks")
    async def submit_block(body: BlockSubmission) -> JSONResponse:
        try:
            raw = bytes.fromhex(body.raw_hex)
        except ValueError as exc:
            raise LedgerError(
                ErrorCode.INVALID_ENCODING, f"raw block is not valid hex: {exc}"
            ) from exc
        app.state.seq += 1
        res = node.submit_raw_block(raw, seq=app.state.seq)
        payload = {
            "accepted": res.accepted,
            "height": res.height,
            "block_hash": res.block_hash,
            "fee_total": res.fee_total,
            "state_unchanged": res.state_unchanged,
        }
        if res.accepted:
            return JSONResponse(status_code=201, content=payload)
        status = _STATUS.get(
            ErrorCategory(res.error["category"]), 500  # type: ignore[index]
        )
        return JSONResponse(
            status_code=status, content={**payload, "error": res.error}
        )

    return app


def _parse_hex(text: str, length: int, what: str, code: ErrorCode) -> bytes:
    try:
        raw = bytes.fromhex(text)
    except ValueError as exc:
        raise LedgerError(code, f"bad {what} hex: {exc}") from exc
    if len(raw) != length:
        raise LedgerError(code, f"{what} must be {length} bytes, got {len(raw)}")
    return raw


def run() -> None:  # pragma: no cover - process entry point
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8080, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    run()
