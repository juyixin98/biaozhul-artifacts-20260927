"""HTTP API for the revocable derived index.

Endpoints
---------
POST /blocks                 submit a sealed block (accept / suspend / reject)
GET  /chain                  active chain summary, tip, finality depth
GET  /chain/block/{hash}     stored block + active/final status
GET  /accounts/{address}     balance, nonce, confirmations of tip
GET  /accounts/{address}/events  derived ledger events (revocable projection)
GET  /transactions/{txid}    every known occurrence + which one contributes
GET  /pending                suspended orphan blocks
GET  /diagnostics            recent decisions with request ids and state
GET  /health                 liveness

Error bodies always carry ``error.code`` (a stable RejectReason), a message,
the request id and key chain state.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..config import Settings
from ..diag.logger import new_request_id
from ..kernel.errors import IngestionError, Outcome
from ..storage.store import SwitchInterrupted
from .deps import build_app_state


async def _safe_json(request: Request):
    # Starlette caches the body after the route read it, so re-parsing is safe.
    try:
        return await request.json()
    except Exception:
        return None


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, state: Optional[dict] = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.state = state or {}


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    producers_path = Path(__file__).resolve().parents[3] / "config" / "authorized_producers.json"
    producer_set = set(json.loads(producers_path.read_text(encoding="utf-8"))["addresses"])

    state = build_app_state(settings, producer_set)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # pragma: no cover - exercised by uvicorn
        # Complete any switch interrupted by a hard process kill.
        state.engine.resume_switch()
        yield
        state.close()

    app = FastAPI(
        title="Revocable chain-derived index",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.exception_handler(IngestionError)
    async def ingestion_error_handler(request: Request, exc: IngestionError):
        rid = getattr(request.state, "request_id", None)
        tip = state.store.active_tip()
        # Stateless rejects are raised before the engine writes a decision;
        # record one here so every accept/reject is auditable by request id.
        try:
            raw = await _safe_json(request)
        except Exception:
            raw = None
        state.diag.record(
            request_id=rid,
            outcome=Outcome.REJECTED.value,
            reason=exc.reason.value,
            height=raw.get("height") if isinstance(raw, dict) else None,
            parent=raw.get("parent") if isinstance(raw, dict) else None,
            weight=(
                int(raw["difficulty"])
                if isinstance(raw, dict) and isinstance(raw.get("difficulty"), int)
                else None
            ),
            detail=exc.detail,
        )
        body = {
            "error": {
                "code": exc.reason.value,
                "message": exc.detail,
                "request_id": rid,
                "state": {
                    "active_tip": tip["hash"] if tip else None,
                    "active_height": int(tip["height"]) if tip else None,
                    **{k: v for k, v in exc.state.items() if isinstance(v, (int, str, bool))},
                },
            }
        }
        return JSONResponse(status_code=422, content=body)

    @app.exception_handler(SwitchInterrupted)
    async def switch_interrupted_handler(request: Request, exc: SwitchInterrupted):
        rid = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "SWITCH_INTERRUPTED",
                    "message": str(exc),
                    "request_id": rid,
                    "hint": "call POST /chain/resume to complete the durable switch",
                }
            },
        )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or new_request_id()
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    # ------------------------------------------------------------- POST
    @app.post("/blocks")
    async def submit_block(request: Request):
        body = await request.json()
        crash_point = None
        if isinstance(body, dict) and body.pop("__crash_point__", None) == "after_detach":
            # Test hook for the interruption test.
            crash_point = "after_detach"
        rid = request.state.request_id
        result = state.engine.ingest(body, request_id=rid, crash_point=crash_point)
        return _ingest_response(result.as_dict(), rid, state)

    @app.post("/chain/resume")
    async def resume_chain(request: Request):
        rid = request.state.request_id
        resumed_switch = state.engine.resume_switch(request_id=rid)
        accepted = state.engine.resume_pending(rid)
        tip = state.store.active_tip()
        return {
            "request_id": rid,
            "resumed_switch": resumed_switch,
            "released_pending": accepted,
            "tip": tip["hash"] if tip else None,
            "height": int(tip["height"]) if tip else None,
        }

    # -------------------------------------------------------------- GET
    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/chain")
    async def chain():
        tip = state.store.active_tip()
        cumulative_weight = (
            state.engine.active_cumulative_weight() if tip else None
        )
        return {
            "tip": tip["hash"] if tip else None,
            "height": int(tip["height"]) if tip else None,
            "weight": cumulative_weight,
            "finality_depth": settings.finality_depth,
            "allowed_difficulties": sorted(settings.allowed_difficulties),
            "active_hashes": state.store.active_hashes(),
            "pending_count": state.store.pending_count(),
        }

    @app.get("/chain/block/{block_hash}")
    async def block_detail(block_hash: str):
        meta = state.store.get_block_meta(block_hash)
        if meta is None:
            raise ApiError(404, "NOT_FOUND", f"block {block_hash[:12]}… unknown")
        confs = state.engine.confirmations(block_hash)
        return {
            "hash": meta["hash"],
            "height": int(meta["height"]),
            "parent": meta["parent"],
            "weight": int(meta["weight"]),
            "producer": meta["producer"],
            "timestamp": meta["timestamp"],
            "on_active": bool(meta["is_active"]),
            "confirmations": confs,
            "final": confs is not None and confs > settings.finality_depth,
        }

    @app.get("/accounts/{address}")
    async def account(address: str):
        return {
            "address": address,
            "balance": state.store.account_balance(address),
            "nonce": state.store.account_nonce(address),
        }

    @app.get("/accounts/{address}/events")
    async def account_events(address: str):
        rows = state.store.events_for_address(address)
        return {
            "address": address,
            "events": [
                {
                    "seq": r["seq"],
                    "block_hash": r["block_hash"],
                    "height": int(r["height"]),
                    "txid": r["txid"],
                    "position_in_block": int(r["position_in_block"]),
                    "kind": r["kind"],
                    "amount_delta": int(r["amount_delta"]),
                    "nonce_delta": int(r["nonce_delta"]),
                }
                for r in rows
            ],
        }

    @app.get("/transactions/{tx_id}")
    async def transaction(tx_id: str):
        rows = state.store.tx_contributions(tx_id)
        if not rows:
            raise ApiError(404, "NOT_FOUND", f"txid {tx_id[:12]}… unknown")
        occurrences = [
            {
                "block_hash": r["block_hash"],
                "height": int(r["height"]),
                "on_active": bool(r["on_active"]),
            }
            for r in rows
        ]
        active = [o for o in occurrences if o["on_active"]]
        return {
            "txid": tx_id,
            "occurrences": occurrences,
            "active_occurrence": active[0]["block_hash"] if active else None,
            "contributes_to_best_chain": bool(active),
        }

    @app.get("/pending")
    async def pending():
        return {
            "pending": [
                {"hash": h, "parent": b["parent"], "height": b["height"], "arrived_seq": s}
                for h, b, s in state.store.all_pending()
            ]
        }

    @app.get("/diagnostics")
    async def diagnostics(limit: int = 50):
        rows = state.store.diagnostics(limit=limit)
        return {
            "diagnostics": [
                {
                    "seq": r["seq"],
                    "request_id": r["request_id"],
                    "outcome": r["outcome"],
                    "reason": r["reason"],
                    "block_hash": r["block_hash"],
                    "height": r["height"],
                    "parent": r["parent"],
                    "active_tip": r["active_tip"],
                    "active_height": r["active_height"],
                    "weight": r["weight"],
                    "detail": r["detail"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        }

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "request_id": getattr(request.state, "request_id", None),
                    "state": exc.state,
                }
            },
        )

    return app


def _ingest_response(payload: dict, rid: str, state) -> dict:
    status_outcome = payload["outcome"]
    if status_outcome == "REJECTED":
        # Engine already persisted diagnostics; surface the same code.
        return JSONResponse(  # type: ignore[return-value]
            status_code=422,
            content={
                "request_id": rid,
                "result": payload,
                "error": {
                    "code": payload["reason"],
                    "message": payload["detail"],
                },
            },
        )
    return {"request_id": rid, "result": payload}
