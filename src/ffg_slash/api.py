"""FastAPI HTTP surface over the slashing detector."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from . import __version__

router = APIRouter()


def _service(request: Request):
    return request.app.state.service


@router.get("/health")
def health(request: Request):
    svc = _service(request)
    return {"status": "ok", "version": __version__,
            "run_id": svc.run_id, "chain_id": svc.chain_id}


@router.post("/votes")
async def submit_vote(request: Request):
    """Submit one vote envelope. Status codes: 201 accepted / 200 duplicate /
    400 rejected (reason named) / 422 malformed JSON or envelope."""
    svc = _service(request)
    raw = await request.body()
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422,
                            detail={"error": "malformed",
                                    "reason": f"not valid JSON: {exc}"})
    if not isinstance(obj, dict):
        raise HTTPException(status_code=422,
                            detail={"error": "malformed",
                                    "reason": "payload must be a JSON object"})
    # ingest_raw performs the single parse + full verified pipeline;
    # malformed envelopes are counted as rejections there, not successes.
    result = svc.ingest_raw(obj)
    body = result.as_dict()
    if result.status.value == "accepted":
        return JSONResponse(status_code=201, content=body)
    if result.status.value == "duplicate":
        return JSONResponse(status_code=200, content=body)
    return JSONResponse(status_code=400, content=body)


@router.get("/evidences")
def evidences(request: Request):
    return {"evidences": _service(request).storage.list_evidences()}


@router.get("/evidences/{evidence_id}")
def evidence(evidence_id: str, request: Request):
    packet = _service(request).storage.get_evidence(evidence_id)
    if packet is None:
        raise HTTPException(status_code=404, detail="evidence not found")
    return packet


@router.get("/stats")
def stats(request: Request):
    return _service(request).stats()


@router.get("/finality")
def finality(request: Request):
    return _service(request).state.snapshot_state()
