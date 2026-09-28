"""FastAPI 验证接口。

端点：
- POST /v1/playlists/{name}/versions  提交一个播放列表版本（原文）
- POST /v1/compare                    对比同一播放列表的两个版本
- POST /v1/plans                      由某版本生成可下载播放计划
- GET  /v1/jobs/{job_id}              查询作业状态

每个响应都带 request_id 与诊断列表；诊断中的 URI 已脱敏。
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .compare import compare_versions
from .config import Settings
from .diagnostics import DiagnosticLog
from .jobs import JobStore
from .models import FailureCategory
from .parser import ParseFailure, parse_playlist
from .planner import build_plan


class CompareRequest(BaseModel):
    name: str
    from_version: int
    to_version: Optional[int] = None  # 缺省取最新


class PlanRequest(BaseModel):
    name: str
    version: Optional[int] = None     # 缺省取最新
    since_sequence: Optional[int] = None


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = JobStore(settings.db_path)
    app = FastAPI(title="hlsplan", version="0.1.0")
    app.state.store = store
    app.state.settings = settings

    def _envelope(log: DiagnosticLog, **payload):
        body = {"request_id": log.request_id, "diagnostics": log.to_list()}
        body.update(payload)
        return body

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        log = DiagnosticLog()
        request.state.log = log
        response = await call_next(request)
        response.headers["X-Request-Id"] = log.request_id
        return response

    @app.post("/v1/playlists/{name}/versions", status_code=201)
    async def submit_version(name: str, request: Request):
        log: DiagnosticLog = request.state.log
        body = await request.body()
        if len(body) > settings.max_playlist_bytes:
            log.error(FailureCategory.PARSE_ERROR.value, "playlist body too large",
                      size=len(body))
            return JSONResponse(status_code=413, content=_envelope(log))
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            log.error(FailureCategory.PARSE_ERROR.value, "body is not valid UTF-8")
            return JSONResponse(status_code=422, content=_envelope(log))

        try:
            snapshot = parse_playlist(text, name=name, log=log)
        except ParseFailure as exc:
            return JSONResponse(
                status_code=422,
                content=_envelope(log, rejected=True,
                                  failure_categories=sorted({
                                      r.code for r in exc.log.records
                                      if r.severity == "ERROR"})),
            )
        version = store.save_version(snapshot)
        log.info("VERSION_STORED", "playlist version stored",
                 name=name, version=version)
        return _envelope(log, name=name, version=version,
                         media_sequence=snapshot.media_sequence,
                         discontinuity_sequence=snapshot.discontinuity_sequence,
                         endlist=snapshot.endlist,
                         segment_count=len(snapshot.segments))

    @app.post("/v1/compare")
    async def compare(req: CompareRequest, request: Request):
        log: DiagnosticLog = request.state.log
        old = store.get_version(req.name, req.from_version)
        if old is None:
            log.error(FailureCategory.NOT_FOUND.value, "from_version not found",
                      name=req.name, version=req.from_version)
            return JSONResponse(status_code=404, content=_envelope(log))
        if req.to_version is None:
            new = store.latest_version(req.name)
        else:
            new = store.get_version(req.name, req.to_version)
        if new is None or new.version == old.version:
            log.error(FailureCategory.NOT_FOUND.value, "to_version not found",
                      name=req.name, version=req.to_version)
            return JSONResponse(status_code=404, content=_envelope(log))

        job_id = store.create_job(
            "compare", log.request_id,
            {"name": req.name, "from": old.version, "to": new.version})
        try:
            diff = compare_versions(
                old, new,
                duration_tolerance=settings.duration_tolerance, log=log)
            store.finish_job(job_id, "DONE", result=diff.to_dict())
        except Exception as exc:  # 兜底：作业状态必须落库
            store.finish_job(job_id, "FAILED", error=str(exc))
            log.error(FailureCategory.JOB_FAILED.value, "compare job failed",
                      job_id=job_id)
            return JSONResponse(status_code=500, content=_envelope(log, job_id=job_id))
        status = 409 if diff.rejected else 200
        return JSONResponse(status_code=status,
                            content=_envelope(log, job_id=job_id,
                                              diff=diff.to_dict()))

    @app.post("/v1/plans")
    async def plan(req: PlanRequest, request: Request):
        log: DiagnosticLog = request.state.log
        snapshot = (store.latest_version(req.name) if req.version is None
                    else store.get_version(req.name, req.version))
        if snapshot is None:
            log.error(FailureCategory.NOT_FOUND.value, "playlist version not found",
                      name=req.name, version=req.version)
            return JSONResponse(status_code=404, content=_envelope(log))

        job_id = store.create_job(
            "plan", log.request_id,
            {"name": req.name, "version": snapshot.version,
             "since_sequence": req.since_sequence})
        try:
            result = build_plan(snapshot, since_sequence=req.since_sequence, log=log)
            store.finish_job(job_id, "DONE", result=result.to_dict())
        except Exception as exc:
            store.finish_job(job_id, "FAILED", error=str(exc))
            log.error(FailureCategory.JOB_FAILED.value, "plan job failed",
                      job_id=job_id)
            return JSONResponse(status_code=500, content=_envelope(log, job_id=job_id))
        return _envelope(log, job_id=job_id, plan=result.to_dict())

    @app.get("/v1/jobs/{job_id}")
    async def job_status(job_id: str, request: Request):
        log: DiagnosticLog = request.state.log
        job = store.get_job(job_id)
        if job is None:
            log.error(FailureCategory.NOT_FOUND.value, "job not found",
                      job_id=job_id)
            return JSONResponse(status_code=404, content=_envelope(log))
        return _envelope(log, job=job)

    return app


app = create_app()
