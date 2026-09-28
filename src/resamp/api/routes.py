"""FastAPI routes: streaming jobs, validation, offline WAV/samples helpers."""
from __future__ import annotations

import base64
import logging

import numpy as np
from fastapi import APIRouter, Request

from ..dsp.fir import design_prototype
from ..dsp.ratios import RationalRatio
from ..errors import InvalidInputError, ResampError
from ..media import read_wav, write_wav
from ..service import decode_samples, encode_samples
from .schemas import (CreateJobRequest, DesignRequest, OfflineSamplesRequest,
                      OfflineWavRequest, SamplesPayload, ValidatePayloadRequest)

log = logging.getLogger("resamp.api")
router = APIRouter()


def service_of(request: Request):
    return request.app.state.service


# --------------------------------------------------------------------- design
@router.post("/v1/design")
def design_filter(req: DesignRequest, request: Request) -> dict:
    """Validate a rate pair and return filter design / delay / padding specs.

    Boundary check used by callers before accepting media: reports the reduced
    rational ratio, Kaiser beta/taps, cutoffs, group delay and the fixed head
    and tail padding.
    """
    settings = service_of(request).settings
    ratio = RationalRatio.reduce(
        req.fin, req.fout, max_rate=settings.max_rate,
        max_factor=settings.max_ratio_factor)
    design = design_prototype(
        ratio, attenuation_db=req.attenuation_db,
        transition_half_width=req.transition_half_width,
        max_taps=settings.max_filter_taps)
    l, m = ratio.l, ratio.m

    def total_count(n_in: int) -> int:
        if n_in <= 0:
            return 0
        return (l * (n_in - 1) + design.half) // m + 1

    return {
        "fin": req.fin, "fout": req.fout, "l": l, "m": m,
        "high_rate": ratio.high_rate,
        "head_pad_input_samples": design.half // l,
        "tail_pad_input_samples": 2 * design.half // l + 2,
        "output_count_example": {
            "n_in": [0, 1, 1000],
            "n_out": [total_count(v) for v in (0, 1, 1000)],
        },
        **design.design_summary(),
    }


# ------------------------------------------------------------------------ jobs
@router.post("/v1/jobs", status_code=201)
def create_job(req: CreateJobRequest, request: Request) -> dict:
    result = service_of(request).create_job(
        fin=req.fin, fout=req.fout, output_dtype=req.output_dtype)
    return result


@router.post("/v1/jobs/{job_id}/push")
def push_samples(job_id: str, payload: SamplesPayload, request: Request) -> dict:
    x = decode_samples(
        {"encoding": payload.encoding, "data": payload.data},
        list_cap=service_of(request).settings.json_list_sample_cap)
    return service_of(request).push(job_id, x)


@router.post("/v1/jobs/{job_id}/flush")
def flush_job(job_id: str, request: Request) -> dict:
    return service_of(request).flush(job_id)


@router.get("/v1/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict:
    return service_of(request).status(job_id)


@router.get("/v1/jobs/{job_id}/chunks")
def get_chunks(job_id: str, request: Request) -> dict:
    return service_of(request).chunks(job_id)


@router.get("/v1/jobs/{job_id}/result")
def get_result(job_id: str, request: Request) -> dict:
    return service_of(request).result(job_id)


# ------------------------------------------------------------------ validation
@router.post("/v1/validate/payload")
def validate_payload(req: ValidatePayloadRequest, request: Request) -> dict:
    """Parse-only boundary check: decode/validate samples for ``fin``.

    Returns the sample count, min/max, finite flag and whether the duration is
    a whole number of samples at the declared rate.  No resampling happens.
    """
    x = decode_samples(
        {"encoding": req.payload.encoding, "data": req.payload.data},
        list_cap=service_of(request).settings.json_list_sample_cap)
    return {
        "valid": True,
        "fin": req.fin,
        "n_samples": int(x.size),
        "duration_seconds": x.size / req.fin if req.fin > 0 else None,
        "min": float(x.min()) if x.size else None,
        "max": float(x.max()) if x.size else None,
        "all_finite": bool(np.all(np.isfinite(x))),
        "dtype": "float64",
    }


# --------------------------------------------------------------------- offline
@router.post("/v1/resample/wav")
def resample_wav(req: OfflineWavRequest, request: Request) -> dict:
    """One-shot: base64 PCM WAV in -> base64 resampled PCM WAV + metadata."""
    try:
        blob = base64.b64decode(req.wav_base64, validate=True)
    except Exception as exc:
        raise InvalidInputError("invalid base64 WAV payload",
                                details={"reason": str(exc)}) from exc
    pcm = read_wav(blob)
    svc = service_of(request)
    y, job = svc.run_offline(
        pcm.samples, fin=pcm.sample_rate, fout=req.fout,
        chunk_size=req.chunk_size)
    wav_bytes = write_wav(y, req.fout, sample_width=req.output_sample_width)
    return {
        "job_id": job["job_id"],
        "state": job["state"],
        "input": {"sample_rate": pcm.sample_rate,
                  "sample_width_bytes": pcm.sample_width,
                  "n_samples": pcm.n_samples},
        "output": {"sample_rate": req.fout,
                   "sample_width_bytes": req.output_sample_width,
                   "n_samples": int(y.size),
                   "encoding": "base64-wav-pcm",
                   "data": base64.b64encode(wav_bytes).decode("ascii")},
    }


@router.post("/v1/resample/samples")
def resample_samples_route(req: OfflineSamplesRequest, request: Request) -> dict:
    """One-shot resample of an encoded float64 payload (for quick checks)."""
    x = decode_samples(
        {"encoding": req.payload.encoding, "data": req.payload.data},
        list_cap=service_of(request).settings.json_list_sample_cap)
    y, job = service_of(request).run_offline(
        x, fin=req.fin, fout=req.fout, output_dtype=req.output_dtype,
        chunk_size=req.chunk_size)
    return {"job_id": job["job_id"], "state": job["state"],
            "n_input": int(x.size), "n_output": int(y.size),
            "output": encode_samples(y)}
