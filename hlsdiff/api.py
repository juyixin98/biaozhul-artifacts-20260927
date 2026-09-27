"""FastAPI 验证接口。

每个响应都带 request_id 与关键状态,说明为什么接受、拒绝或无法判定;
日志中的 URI 一律脱敏(去查询串)。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .compare import compare_versions
from .errors import FailureCategory, PlaylistParseError
from .parser import parse_playlist
from .plan import build_plan
from .store import Store

logger = logging.getLogger("hlsdiff.api")


class IngestBody(BaseModel):
    content: str  # 原始 M3U8 文本


def _state_summary(pl) -> dict:
    return {
        "media_sequence_range": [pl.first_sequence, pl.last_sequence],
        "segment_count": len(pl.segments),
        "discontinuity_sequence": pl.discontinuity_sequence,
        "endlist": pl.endlist,
    }


def create_app(db_path: str = ":memory:") -> FastAPI:
    app = FastAPI(title="hlsdiff", version="0.1.0")
    store = Store(db_path)
    app.state.store = store

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex[:12]
        return await call_next(request)

    @app.post("/streams/{stream_id}/playlists", status_code=201)
    def ingest(stream_id: str, body: IngestBody, request: Request):
        rid = request.state.request_id
        try:
            playlist = parse_playlist(body.content)
        except PlaylistParseError as exc:
            logger.info(
                "ingest rejected request_id=%s stream=%s category=%s line=%s",
                rid, stream_id, exc.category.value, exc.line_no,
            )
            return JSONResponse(
                status_code=422,
                content={
                    "request_id": rid,
                    "accepted": False,
                    "category": exc.category.value,
                    "detail": exc.detail,
                    "line_no": exc.line_no,
                },
            )
        version_no = store.add_snapshot(stream_id, body.content, playlist)
        logger.info(
            "ingest accepted request_id=%s stream=%s version=%d state=%s",
            rid, stream_id, version_no, _state_summary(playlist),
        )
        return {
            "request_id": rid,
            "accepted": True,
            "version_no": version_no,
            "state": _state_summary(playlist),
        }

    @app.post("/streams/{stream_id}/compare")
    def compare(
        stream_id: str,
        request: Request,
        from_version: int | None = Query(default=None),
        to_version: int | None = Query(default=None),
    ):
        rid = request.state.request_id
        versions = store.list_versions(stream_id)
        if not versions:
            return _error(404, FailureCategory.STREAM_NOT_FOUND, rid, stream_id)
        old_no = from_version if from_version is not None else (
            versions[-2] if len(versions) >= 2 else None
        )
        new_no = to_version if to_version is not None else versions[-1]
        if old_no is None:
            return _error(
                422, FailureCategory.VERSION_NOT_FOUND, rid, stream_id,
                "仅有一个版本,无法对比",
            )
        old = store.get_snapshot(stream_id, old_no)
        new = store.get_snapshot(stream_id, new_no)
        if old is None or new is None:
            return _error(404, FailureCategory.VERSION_NOT_FOUND, rid, stream_id)

        job_id = store.create_job(stream_id, "compare", rid)
        report = compare_versions(old, new)
        store.finish_job(job_id, "DONE", result=report)
        logger.info(
            "compare request_id=%s job=%s stream=%s %d->%d decision=%s reasons=%s",
            rid, job_id, stream_id, old_no, new_no, report.decision, report.reasons,
        )
        return {
            "request_id": rid,
            "job_id": job_id,
            "from_version": old_no,
            "to_version": new_no,
            "decision": report.decision,
            "report": asdict(report),
            "old_state": _state_summary(old),
            "new_state": _state_summary(new),
        }

    @app.get("/streams/{stream_id}/plan")
    def plan(stream_id: str, request: Request, version: int | None = Query(default=None)):
        rid = request.state.request_id
        playlist = store.get_snapshot(stream_id, version)
        if playlist is None:
            category = (
                FailureCategory.VERSION_NOT_FOUND
                if version is not None and store.list_versions(stream_id)
                else FailureCategory.STREAM_NOT_FOUND
            )
            return _error(404, category, rid, stream_id)
        job_id = store.create_job(stream_id, "plan", rid)
        playback_plan = build_plan(playlist)
        store.finish_job(job_id, "DONE", result={
            "total_duration": playback_plan.total_duration,
            "entries": len(playback_plan.entries),
            "boundaries": len(playback_plan.boundaries),
        })
        logger.info(
            "plan request_id=%s job=%s stream=%s entries=%d boundaries=%d",
            rid, job_id, stream_id, len(playback_plan.entries),
            len(playback_plan.boundaries),
        )
        return {
            "request_id": rid,
            "job_id": job_id,
            "total_duration": playback_plan.total_duration,
            "diagnostics": playback_plan.diagnostics,
            "entries": [asdict(e) for e in playback_plan.entries],
            "boundaries": [asdict(b) for b in playback_plan.boundaries],
        }

    @app.get("/jobs/{job_id}")
    def job(job_id: str, request: Request):
        rid = request.state.request_id
        record = store.get_job(job_id)
        if record is None:
            return _error(404, FailureCategory.JOB_NOT_FOUND, rid, job_id)
        return {"request_id": rid, "job": record}

    return app


def _error(status: int, category: FailureCategory, request_id: str, subject: str,
           detail: str | None = None) -> JSONResponse:
    logger.info(
        "error request_id=%s category=%s subject=%s", request_id, category.value, subject
    )
    return JSONResponse(
        status_code=status,
        content={
            "request_id": request_id,
            "category": category.value,
            "subject": subject,
            "detail": detail or category.value,
        },
    )


app = create_app()
