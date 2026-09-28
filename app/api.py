"""FastAPI wiring (thin). All security decisions live in the kernel/parsing.

Endpoints
---------
POST /collections            create a collection + split a secret
GET  /collections/{cid}      collection metadata + stored share fingerprints
POST /collections/{cid}/recover
                             submit shares -> threshold recovery
GET  /collections/{cid}/audit|/audit
                             fingerprint-only audit records

A middleware assigns/propagates a ``X-Request-ID`` so every accept/reject can
be correlated with an audit event.
"""
from __future__ import annotations

import uuid

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .audit import Auditor
from .config import Settings
from .core.kernel import Kernel
from .core.shamir import RecoverStatus, SplitError
from .state import CollectionNotFound, Store


class CreateRequest(BaseModel):
    secret_hex: str = Field(..., description="the secret, hex-encoded")
    threshold: int = Field(..., ge=1, le=64)
    total: int = Field(..., ge=1, le=64)
    collection_id: str | None = None

    @field_validator("secret_hex")
    @classmethod
    def _hex(cls, v: str) -> str:
        if v is None or v == "":
            return ""  # empty secret is legal
        int(v, 16)  # raises if not hex
        if len(v) % 2:
            raise ValueError("hex string must have even length")
        return v


class RecoverRequest(BaseModel):
    shares: list[dict]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(
        title="Threshold Secret Sharing Service",
        version="1.0.0",
        description="Shamir over GF(secp256k1) with independent HMAC integrity.",
    )
    app.state.settings = settings
    store = Store(settings.db_path)
    app.state.store = store
    app.state.kernel = Kernel(store, Auditor(store, to_stderr=settings.audit_to_stderr))

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:16]}"
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    def _rid(request: Request) -> str:
        return getattr(request.state, "request_id", "req_unknown")

    @app.post("/collections", status_code=201)
    def create_collection(request: Request, body: CreateRequest):
        kernel: Kernel = request.app.state.kernel
        secret = bytes.fromhex(body.secret_hex)
        if body.total > settings.max_total_shares:
            raise HTTPException(400, f"total exceeds max {settings.max_total_shares}")
        try:
            out = kernel.create_collection(
                request_id=_rid(request),
                secret=secret,
                threshold=body.threshold,
                total=body.total,
                collection_id=body.collection_id,
            )
        except SplitError as exc:
            raise HTTPException(400, str(exc))
        except TypeError as exc:
            raise HTTPException(400, str(exc))
        out["request_id"] = _rid(request)
        return out

    @app.get("/collections/{collection_id}")
    def get_collection(request: Request, collection_id: str):
        kernel: Kernel = request.app.state.kernel
        try:
            envelopes = kernel.get_collection_bundle(collection_id)
        except CollectionNotFound:
            raise HTTPException(404, "collection not found")
        return {
            "collection_id": collection_id,
            "share_count": len(envelopes),
            "shares": [e.public_view() for e in envelopes],
            "request_id": _rid(request),
        }

    @app.post("/collections/{collection_id}/recover")
    def recover(request: Request, collection_id: str, body: RecoverRequest):
        kernel: Kernel = request.app.state.kernel
        rid = _rid(request)
        try:
            report = kernel.recover(
                request_id=rid,
                collection_id=collection_id,
                submitted=body.shares,
            )
        except CollectionNotFound:
            raise HTTPException(404, "collection not found")

        accepted = report.status in {
            RecoverStatus.RECOVERED_VERIFIED,
            RecoverStatus.RECOVERED_UNVERIFIABLE,
        }
        response_payload = {
            "request_id": rid,
            "collection_id": collection_id,
            "status": report.status.value,
            "accepted": accepted,
            "distinct_xs": report.distinct_xs,
            "used_xs": report.used_xs,
            "extra_xs": report.extra_xs,
            "mismatched_xs": report.mismatched_xs,
            "rejected_shares": report.rejected,
            "diagnostic": (report.math.detail if report.math else
                           f"{len(report.distinct_xs)} distinct admissible < threshold"),
        }
        if accepted and report.secret is not None:
            response_payload["secret_hex"] = report.secret_hex()
        # Success is 200; a categorical failure is still 200 with accepted=false
        # so clients read the structured reason (audit already recorded it).
        return response_payload

    @app.get("/collections/{collection_id}/audit")
    def collection_audit(request: Request, collection_id: str, limit: int = 100):
        auditor: Auditor = request.app.state.kernel.auditor
        rows = auditor.query(collection_id=collection_id, limit=limit)
        return {"request_id": _rid(request), "events": rows}

    @app.get("/audit")
    def audit(request: Request, request_id: str | None = None, limit: int = 100):
        auditor: Auditor = request.app.state.kernel.auditor
        rows = auditor.query(request_id=request_id, limit=limit)
        return {"request_id": _rid(request), "events": rows}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


# ASGI entry point for `uvicorn app.main:app`
app = create_app()
