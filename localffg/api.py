"""FastAPI HTTP layer.

Endpoints:
    GET  /health                       versions + chain info
    POST /v1/validators/bootstrap      seed a synthetic validator (dev/local)
    POST /v1/validators/{id}/weight    epoch weight update
    POST /v1/votes                     submit a signed vote
    GET  /v1/votes/stats               exact category counts
    GET  /v1/evidence                  list evidence
    GET  /v1/evidence/{evidence_id}    one bundle
    POST /v1/evidence/{id}/recheck     independent re-verification
    POST /v1/replay                    offline replay + consistency check

Invalid submissions are returned with the exact category (and HTTP 422),
never collapsed to 200/success.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import PROTOCOL_VERSION, __version__
from .config import AppConfig
from .crypto import Signer
from .logging_utils import new_run_id
from .models import INVALID_STATUSES, SignedVote, VoteStatus
from .service import VoteService


class VoteIn(BaseModel):
    chain_id: str
    validator_id: str
    source_round: int = Field(ge=0)
    target_round: int = Field(ge=1)
    block_root: str  # hex
    signer_pubkey: str  # hex
    signature: str  # hex
    run_id: str | None = None


class BootstrapIn(BaseModel):
    validator_id: str
    weight: int = Field(ge=0)
    effective_epoch: int = Field(default=0, ge=0)
    seed: str | None = None  # deterministic local key material


class WeightIn(BaseModel):
    effective_epoch: int = Field(ge=0)
    weight: int = Field(ge=0)


def create_app(config: AppConfig, *, service: VoteService | None = None) -> FastAPI:
    app = FastAPI(
        title="localffg double/surround vote detector",
        version=__version__,
        description="Local synthetic consensus slashing evidence — FastAPI + SQLite.",
    )
    svc = service or VoteService(config)
    app.state.service = svc
    app.state.config = config

    @app.exception_handler(Exception)
    async def _unhandled(request, exc):  # never report unknown errors as success
        svc.logger.error("http_unhandled_exception", path=request.url.path, error=f"{type(exc).__name__}: {exc}")
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "internal_error", "detail": f"{type(exc).__name__}: {exc}"},
        )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "up",
            "app_version": __version__,
            "protocol_version": PROTOCOL_VERSION,
            "chain_id": config.chain_id,
            "epoch_length": config.epoch_length,
            "run_id": svc.logger.run_id,
        }

    @app.post("/v1/validators/bootstrap")
    def bootstrap(body: BootstrapIn) -> dict[str, Any]:
        if not config.allow_bootstrap_api:
            raise HTTPException(status_code=403, detail="bootstrap API disabled in config")
        if svc.registry.exists(body.validator_id):
            raise HTTPException(status_code=409, detail=f"validator {body.validator_id} exists")
        seed = body.seed.encode("utf-8") if body.seed else f"localffg-seed/{body.validator_id}".encode()
        signer = Signer.from_seed(body.validator_id, seed)
        svc.register_validator(body.validator_id, signer, body.weight, body.effective_epoch)
        return {
            "ok": True,
            "validator_id": body.validator_id,
            "signer_pubkey": signer.public_key_bytes.hex(),
            "weight": body.weight,
            "effective_epoch": body.effective_epoch,
        }

    @app.post("/v1/validators/{validator_id}/weight")
    def update_weight(validator_id: str, body: WeightIn) -> dict[str, Any]:
        if not svc.registry.exists(validator_id):
            raise HTTPException(status_code=404, detail=f"unknown validator {validator_id}")
        svc.set_weight(validator_id, body.effective_epoch, body.weight)
        return {"ok": True, "validator_id": validator_id, "effective_epoch": body.effective_epoch, "weight": body.weight}

    @app.post("/v1/votes")
    def submit_vote(body: VoteIn) -> dict[str, Any]:
        run_id = body.run_id or new_run_id("api")
        try:
            data = body.model_dump(exclude={"run_id"})
            signed = SignedVote.from_json_dict(data)
        except Exception as exc:
            svc.logger.warn("vote_malformed_envelope", run_id=run_id, error=str(exc))
            raise HTTPException(status_code=422, detail={"category": VoteStatus.MALFORMED.value, "reason": str(exc)})

        outcome = svc.submit(signed, run_id=run_id)
        payload = {
            "ok": outcome.status not in INVALID_STATUSES,
            "seq": outcome.seq,
            "run_id": outcome.run_id,
            "category": outcome.status.value,
            "reason": outcome.reason,
            "slashable": outcome.slashable,
            "evidence": outcome.evidence,
        }
        if outcome.status in INVALID_STATUSES:
            return JSONResponse(status_code=422, content=payload)
        return payload

    @app.get("/v1/votes/stats")
    def votes_stats() -> dict[str, Any]:
        return svc.stats()

    @app.get("/v1/evidence")
    def list_evidence() -> dict[str, Any]:
        return {"count": len(svc.list_evidence()), "evidence": svc.list_evidence()}

    @app.get("/v1/evidence/{evidence_id}")
    def get_evidence(evidence_id: str) -> dict[str, Any]:
        bundle = svc.get_evidence(evidence_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail="evidence not found")
        return bundle

    @app.post("/v1/evidence/{evidence_id}/recheck")
    def recheck(evidence_id: str) -> dict[str, Any]:
        report = svc.check_evidence(evidence_id)
        if report is None:
            raise HTTPException(status_code=404, detail="evidence not found")
        return report

    @app.post("/v1/replay")
    def replay() -> dict[str, Any]:
        return svc.replay()

    return app


def build_default_app() -> FastAPI:
    from .config import load_config

    return create_app(load_config())
