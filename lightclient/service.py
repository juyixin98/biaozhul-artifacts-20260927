"""FastAPI service boundary.

Exposes the kernel over HTTP. The service is intentionally dumb: it parses
JSON, translates the typed error taxonomy into the documented status codes
and error envelopes, and serializes results. All safety logic stays in the
kernel.

Error envelope (every non-2xx response):
    {"ok": false, "error": {"code", "category", "reason", "detail"}}

Status mapping: input=400, state=409, resource=413, compute=500.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import codec
from .config import LightClientConfig
from .errors import CATEGORY_HTTP_STATUS, LightClientError, ResourceLimit
from .kernel import LightClientKernel
from .replay import ReplayEngine, ReplayItem, item_from_wire
from .store import Store


def error_response(exc: LightClientError) -> JSONResponse:
    status = CATEGORY_HTTP_STATUS[exc.category]
    return JSONResponse(status_code=status, content=exc.to_dict())


def _hex_field(doc: dict[str, Any], name: str, length: int | None = None) -> bytes:
    if name not in doc or not isinstance(doc[name], str):
        from .errors import InputMalformed

        raise InputMalformed(f"missing or non-string field: {name}")
    return codec.unhex(doc[name], name, length)


def create_app(
    *,
    store_path: str,
    config: LightClientConfig | None = None,
    trusted_checkpoint_key: bytes,
    run_id: str | None = None,
) -> FastAPI:
    cfg = config or LightClientConfig.from_env()
    store = Store(store_path)
    kernel = LightClientKernel(
        store, cfg, trusted_checkpoint_key, run_id=run_id
    )
    engine = ReplayEngine(kernel)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        yield
        store.close()

    app = FastAPI(
        title="Local test-chain header light client",
        version="1.0.0",
        description=(
            "Simplified header light client for a fixed LOCAL test chain. "
            "Not compatible with any public blockchain."
        ),
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def bound_body(request: Request, call_next: Any) -> Any:
        # Fail fast with the documented resource category instead of
        # letting an oversized payload hit uvicorn's generic 413/422.
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > cfg.max_request_bytes:
                    return error_response(
                        ResourceLimit(
                            "request body exceeds configured maximum",
                            {
                                "content_length": int(cl),
                                "limit": cfg.max_request_bytes,
                            },
                        )
                    )
            except ValueError:
                pass
        return await call_next(request)

    @app.exception_handler(LightClientError)
    async def _lc_error(_request: Request, exc: LightClientError) -> JSONResponse:
        return error_response(exc)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        initialized = kernel.is_initialized()
        body: dict[str, Any] = {
            "ok": True,
            "protocol": "local-header-lightclient/v1",
            "initialized": initialized,
        }
        if initialized:
            body["tip"] = kernel.tip().to_dict()
            body["chain_id"] = kernel.chain_id()
            body["trust_period_seconds"] = kernel.trust_period()
        return body

    @app.post("/bootstrap")
    async def bootstrap(request: Request) -> dict[str, Any]:
        doc = await _json(request)
        env_wire = _hex_field(doc, "checkpoint_envelope")
        if len(env_wire) > cfg.max_request_bytes:
            raise ResourceLimit(
                "envelope too large",
                {"size": len(env_wire), "limit": cfg.max_request_bytes},
            )
        envelope = codec.decode_envelope(
            env_wire, max_committee_members=cfg.max_committee_members
        )
        tip = kernel.bootstrap(envelope)
        return {"ok": True, "tip": tip.to_dict()}

    @app.post("/headers")
    async def submit_header(request: Request) -> dict[str, Any]:
        doc = await _json(request)
        header_wire = _hex_field(doc, "header")
        cert_wire = _hex_field(doc, "certificate")
        run_id = doc.get("run_id")
        result = kernel.apply_header_wire(header_wire, cert_wire, run_id=run_id)
        return {"ok": True, "result": result.to_dict()}

    @app.post("/replay")
    async def replay(request: Request) -> dict[str, Any]:
        doc = await _json(request)
        raw_items = doc.get("items")
        if not isinstance(raw_items, list):
            from .errors import InputMalformed

            raise InputMalformed("items must be a list")
        run_id = doc.get("run_id")
        items: list[ReplayItem] = []
        for i, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                from .errors import InputMalformed

                raise InputMalformed(f"item {i} must be an object")
            items.append(
                item_from_wire(
                    _hex_field(raw, "header"),
                    _hex_field(raw, "certificate"),
                    source=str(raw.get("source", f"item-{i}")),
                )
            )
        report = engine.replay(items, run_id=run_id)
        return {"ok": True, "report": report.to_dict()}

    @app.get("/tip")
    async def tip() -> dict[str, Any]:
        return {"ok": True, "tip": kernel.tip().to_dict()}

    @app.get("/headers/{digest_hex}")
    async def get_header(digest_hex: str) -> dict[str, Any]:
        digest = codec.unhex(digest_hex, "digest", 32)
        header = store.get_header(digest)
        from .errors import ParentUnknown

        if header is None:
            raise ParentUnknown(
                "header not found on the trusted chain",
                {"digest": digest_hex},
            )
        return {
            "ok": True,
            "header": codec.encode_header(header).hex(),
            "digest": digest_hex,
        }

    @app.get("/audit")
    async def audit(limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(limit, 1000))
        return {"ok": True, "entries": store.list_audit(limit)}

    return app


async def _json(request: Request) -> dict[str, Any]:
    from .errors import InputMalformed

    try:
        body = await request.body()
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InputMalformed("request body must be UTF-8 JSON") from None
    if not isinstance(doc, dict):
        raise InputMalformed("request body must be a JSON object")
    return doc


def create_app_from_env() -> FastAPI:
    """Factory used by ``uvicorn lightclient.service:app`` / ``serve``."""
    cfg = LightClientConfig.from_env()
    db_path = os.environ.get("LC_DB_PATH", "./data/lightclient.db")
    key_hex = os.environ.get("LC_CHECKPOINT_KEY_HEX")
    if not key_hex:
        raise RuntimeError(
            "LC_CHECKPOINT_KEY_HEX must be set (32-byte Ed25519 public key, hex)"
        )
    key = codec.unhex(key_hex, "LC_CHECKPOINT_KEY_HEX", 32)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return create_app(store_path=db_path, config=cfg, trusted_checkpoint_key=key)
