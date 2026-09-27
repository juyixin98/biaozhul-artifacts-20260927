"""FastAPI service: validation endpoints, request correlation, diagnostics.

Run:  uvicorn app.main:app --reload
Every response/error carries the service version and a request id so logs
and results can be correlated. Failure causes and uncertain conclusions are
returned as dedicated fields rather than buried in error strings.
"""
from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .analysis import compare
from .config import JitterConfig
from .engine import TraceEvent, run_comparison
from .store import JobStore, result_dto
from fixtures import ALL_FIXTURES, build as build_fixture

logger = logging.getLogger("jitterbuffer")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

DB_PATH = os.environ.get("JB_DB_PATH", "jobs.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = JobStore(DB_PATH)
    logger.info("job store opened path=%s", DB_PATH)
    yield
    app.state.store.close()


app = FastAPI(
    title="Offline RTP Jitter Buffer",
    version=__version__,
    description="Offline jitter buffering and playout scheduling backend.",
    lifespan=lifespan)


# ------------------------------------------------------------------ schemas
class AnalyzeRequest(BaseModel):
    fixture: str = Field(..., description=f"one of {ALL_FIXTURES}")
    client_ref: Optional[str] = Field(None, max_length=128)


class PacketIn(BaseModel):
    seq: int = Field(..., ge=0, le=0xFFFF)
    timestamp: int = Field(..., ge=0, le=0xFFFFFFFF)
    ssrc: int = Field(..., ge=0, le=0xFFFFFFFF)
    arrival_ms: float = Field(..., ge=0)
    payload_hex: Optional[str] = None
    marker: bool = False


class TraceAnalyzeRequest(BaseModel):
    packets: List[PacketIn] = Field(..., min_length=1)
    client_ref: Optional[str] = Field(None, max_length=128)
    config: Optional[Dict[str, Any]] = None


# ------------------------------------------------------------------ helpers
def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req-{uuid.uuid4().hex[:12]}"


def _envelope(request_id: str, **payload: Any) -> Dict[str, Any]:
    return {"version": __version__, "request_id": request_id, **payload}


def _run_job(store: JobStore, source: str, events: List[TraceEvent],
             cfg: JitterConfig, request_id: str,
             client_ref: Optional[str]) -> Dict[str, Any]:
    job_id = store.create_job(source=source, config=cfg.__dict__,
                              request_id=request_id, client_ref=client_ref)
    logger.info("job_start job_id=%s source=%s request_id=%s",
                job_id, source, request_id)
    store.mark_running(job_id)
    try:
        results = run_comparison(events, cfg)
        for mode, res in results.items():
            store.save_run(job_id, mode, result_dto(res.to_dict()))
        verdict = compare(results["adaptive"], results["fixed"])
        store.mark_completed(job_id, verdict["comparison"])
        logger.info("job_done job_id=%s gaps(adaptive=%d fixed=%d)",
                    job_id, verdict["adaptive"]["gap_items"],
                    verdict["fixed_baseline"]["gap_items"])
        return {"job_id": job_id, "verdict": verdict}
    except Exception as exc:  # pragma: no cover - defensive boundary
        logger.exception("job_failed job_id=%s", job_id)
        store.mark_failed(job_id, f"{type(exc).__name__}: {exc}")
        raise


# ------------------------------------------------------------------ routes
@app.get("/health")
def health(request: Request):
    rid = _request_id(request)
    return _envelope(rid, status="ok", db=DB_PATH)


@app.get("/api/fixtures")
def list_fixtures(request: Request):
    rid = _request_id(request)
    specs = [build_fixture(name) for name in ALL_FIXTURES]
    return _envelope(rid, fixtures=[
        {"name": s.name, "description": s.description,
         "expected_packets": s.expected_packets,
         "lost_seqs": s.lost_seqs, "duplicated_seqs": s.duplicated_seqs,
         "notes": s.notes}
        for s in specs])


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest, request: Request):
    rid = _request_id(request)
    if req.fixture not in ALL_FIXTURES:
        return JSONResponse(status_code=404, content=_envelope(
            rid, error="UNKNOWN_FIXTURE",
            detail=f"fixture {req.fixture!r} not found",
            available=ALL_FIXTURES))
    cfg = JitterConfig.from_env()
    spec = build_fixture(req.fixture, cfg)
    out = _run_job(request.app.state.store, f"fixture:{req.fixture}",
                   spec.events, cfg, rid, req.client_ref)
    return _envelope(rid, fixture=req.fixture, **out)


@app.post("/api/analyze/trace")
def analyze_trace(req: TraceAnalyzeRequest, request: Request):
    rid = _request_id(request)
    cfg_kwargs = {}
    if req.config:
        allowed = set(JitterConfig.__dataclass_fields__)
        bad = set(req.config) - allowed
        if bad:
            return JSONResponse(status_code=422, content=_envelope(
                rid, error="UNKNOWN_CONFIG_KEYS",
                detail=f"unsupported config keys: {sorted(bad)}"))
        cfg_kwargs = req.config
    cfg = JitterConfig(**cfg_kwargs)
    events = []
    for p in req.packets:
        payload = bytes.fromhex(p.payload_hex) if p.payload_hex else b""
        events.append(TraceEvent(seq=p.seq, timestamp=p.timestamp,
                                 ssrc=p.ssrc, arrival_ms=p.arrival_ms,
                                 payload=payload, marker=p.marker, raw=None))
    out = _run_job(request.app.state.store, "uploaded_trace",
                   events, cfg, rid, req.client_ref)
    return _envelope(rid, **out)


@app.get("/api/jobs")
def jobs(request: Request, limit: int = 50):
    rid = _request_id(request)
    return _envelope(rid, jobs=request.app.state.store.list_jobs(limit))


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str, request: Request):
    rid = _request_id(request)
    job = request.app.state.store.get_job(job_id)
    if job is None:
        return JSONResponse(status_code=404,
                            content=_envelope(rid, error="JOB_NOT_FOUND",
                                              detail=job_id))
    job["events"] = request.app.state.store.get_events(job_id)
    return _envelope(rid, job=job)


@app.get("/api/jobs/{job_id}/runs/{mode}")
def job_run(job_id: str, mode: str, request: Request):
    rid = _request_id(request)
    if mode not in ("adaptive", "fixed"):
        return JSONResponse(status_code=404,
                            content=_envelope(rid, error="UNKNOWN_MODE",
                                              detail=mode))
    run = request.app.state.store.get_run(job_id, mode)
    if run is None:
        return JSONResponse(status_code=404,
                            content=_envelope(rid, error="RUN_NOT_FOUND",
                                              detail=f"{job_id}/{mode}"))
    return _envelope(rid, run=run)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):  # pragma: no cover
    rid = _request_id(request)
    logger.exception("unhandled_error request_id=%s", rid)
    return JSONResponse(status_code=500, content=_envelope(
        rid, error="INTERNAL_ERROR", detail=f"{type(exc).__name__}: {exc}"))
