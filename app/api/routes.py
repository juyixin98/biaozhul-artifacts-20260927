"""HTTP interface. Error semantics:
  201  validation finished (check repair.status for the actual outcome)
  422  input could not be parsed, or solver configuration was rejected
       (body: {"error": {code, message, ...}, "job_id"} and the job is FAILED)
  404  unknown job id ({"error": {"code": "JOB_NOT_FOUND"}})
Unknown/exceptional states are never reported as success.
"""
import platform
import uuid

import fastapi
import numpy
import pydantic
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import Settings
from app.errors import SubtitleParseError
from app.jobs.store import JobStore
from app.kernel.solver import SolverInputError
from app.service import ValidationOptions, run_validation
from app.version import APP_VERSION
from app.api.schemas import ValidateRequest


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="subtitle-validator", version=APP_VERSION)
    store = JobStore(settings.db_path)
    app.state.store = store
    app.state.settings = settings

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": APP_VERSION}

    @app.get("/v1/meta")
    def meta():
        return {
            "app_version": APP_VERSION,
            "python": platform.python_version(),
            "numpy": numpy.__version__,
            "fastapi": fastapi.__version__,
            "pydantic": pydantic.VERSION,
        }

    @app.post("/v1/validations", status_code=201)
    def create_validation(req: ValidateRequest):
        job_id = uuid.uuid4().hex
        run_id = uuid.uuid4().hex[:12]
        options = ValidationOptions(
            min_duration_ms=req.options.min_duration_ms,
            min_gap_ms=req.options.min_gap_ms,
            media_duration_ms=req.media_duration_ms,
            budget_ms=req.options.budget_ms,
            resolution_ms=req.options.resolution_ms,
            allow_approximate=req.options.allow_approximate,
            max_grid=req.options.max_grid or settings.max_grid,
        )
        store.create_job(job_id=job_id, fmt=req.format, content=req.content,
                         options=options.__dict__, run_id=run_id)
        store.transition(job_id, "RUNNING", "validation started")
        try:
            result = run_validation(req.content, req.format, options)
        except SubtitleParseError as exc:
            err = exc.to_dict()
            store.fail(job_id, err)
            return JSONResponse(status_code=422,
                                content={"error": {**err, "job_id": job_id}})
        except SolverInputError as exc:
            err = {"code": exc.code, "message": exc.message, "details": exc.details}
            store.fail(job_id, err)
            return JSONResponse(status_code=422,
                                content={"error": {**err, "job_id": job_id}})
        store.complete(job_id, result)
        return {"job_id": job_id, "run_id": run_id, "status": "DONE", **result}

    @app.get("/v1/validations/{job_id}")
    def get_validation(job_id: str):
        job = store.get(job_id)
        if job is None:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "JOB_NOT_FOUND",
                                   "message": f"no job with id {job_id!r}"}},
            )
        return job

    return app
