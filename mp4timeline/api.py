"""FastAPI surface: health/versions, parse jobs, and a direct validation
endpoint.  Errors keep their category — failures are never reported as ok."""

from __future__ import annotations

import hashlib
import os
import sys

import fastapi
import numpy
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .boxes import parse_movie
from .config import SERVICE_NAME, SERVICE_VERSION, Settings
from .errors import InputError, MP4Error
from .jobs import JobStore, run_parse_job
from .timeline import build_movie_timeline, movie_timeline_to_dict


class JobRequest(BaseModel):
    path: str


def _error_body(exc: MP4Error) -> dict:
    return {"ok": False, "category": exc.category, "detail": str(exc)}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = JobStore(settings.db_path)
    app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION)
    app.state.store = store
    app.state.settings = settings

    @app.get("/health")
    def health() -> dict:
        return {
            "service": SERVICE_NAME,
            "service_version": SERVICE_VERSION,
            "python": sys.version.split()[0],
            "fastapi": fastapi.__version__,
            "numpy": numpy.__version__,
        }

    @app.post("/jobs", status_code=201)
    def create_job(req: JobRequest) -> dict:
        job_id = store.create(req.path)
        run_parse_job(store, job_id, req.path, settings.max_file_bytes)
        job = store.get(job_id)
        body = {"job_id": job_id, "status": job["status"]}
        if job["status"] == "failed":
            body["error"] = {"category": job["error_category"], "detail": job["error_message"]}
        return body

    @app.get("/jobs")
    def list_jobs() -> list[dict]:
        with store._lock:
            rows = store._conn.execute(
                "SELECT id, path, status, error_category, created, finished FROM jobs ORDER BY created"
            ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"unknown job id {job_id!r}")
        job["events"] = store.events(job_id)
        return job

    @app.get("/jobs/{job_id}/timeline")
    def get_timeline(job_id: str) -> dict:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"unknown job id {job_id!r}")
        if job["status"] != "done":
            raise HTTPException(
                status_code=409,
                detail={
                    "status": job["status"],
                    "error_category": job["error_category"],
                    "error_message": job["error_message"],
                },
            )
        return store.result(job_id)

    @app.post("/validate")
    def validate(req: JobRequest):
        """Synchronous validation: parse + build timeline, report category on
        failure (HTTP 422) without creating a job."""
        try:
            if not os.path.isfile(req.path):
                raise InputError(f"input file not found: {req.path}")
            size = os.path.getsize(req.path)
            if size > settings.max_file_bytes:
                raise InputError(f"input file {size} bytes exceeds limit {settings.max_file_bytes}")
            with open(req.path, "rb") as fh:
                data = fh.read()
            movie = parse_movie(data)
            timeline = build_movie_timeline(movie)
        except MP4Error as exc:
            return JSONResponse(status_code=422, content=_error_body(exc))
        return {
            "ok": True,
            "input_sha256": hashlib.sha256(data).hexdigest(),
            "input_size": len(data),
            "movie_timescale": timeline.movie_timescale,
            "tracks": [
                {
                    "track_id": t.track_id,
                    "handler": t.handler,
                    "media_timescale": t.media_timescale,
                    "samples": len(t.samples),
                    "presentations": len(t.presentations),
                    "gaps": len(t.gaps),
                }
                for t in timeline.tracks
            ],
            "timeline": movie_timeline_to_dict(timeline),
        }

    return app


app = create_app()
