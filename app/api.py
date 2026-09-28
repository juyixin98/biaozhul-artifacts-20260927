"""FastAPI 应用与路由。

错误映射：所有 :class:`ServiceError` 统一渲染成
``{"error": {"code", "category", "message", "details"}}``，类别/状态码见
:mod:`app.errors`。``run_id`` 通过响应头 ``X-Run-Id`` 返回，便于取诊断。
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .diagnostics import Diag
from .errors import ServiceError
from .schemas import (
    ApplyResult,
    DiagnosticsOut,
    PlanDetail,
    PlanRequest,
    PlanSummary,
    RuleIn,
    RuleValidationOut,
    SourceIn,
    SourceSpecOut,
    SourceVersionOut,
)
from .service import Service
from .storage import Repository

_DB_PATH = os.environ.get("NRP_DB_PATH", "./data/nrp.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    repo = Repository(_DB_PATH)
    app.state.repo = repo
    app.state.service = Service(repo)
    yield
    repo.close()


app = FastAPI(
    title="非重叠替换规划服务",
    version="1.0.0",
    description="基于无回溯正则 (RE2) 的大文本非重叠替换规划、版本存储与流式应用。",
    lifespan=lifespan,
)


@app.exception_handler(ServiceError)
async def service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


def _service(request: Request) -> Service:
    return request.app.state.service


def _diag(request: Request) -> Diag:
    return Diag(request.app.state.repo)


@app.post("/sources", response_model=SourceVersionOut, status_code=201)
def create_source(payload: SourceIn, request: Request) -> SourceVersionOut:
    rec = _service(request).upload_source(payload.text)
    return SourceVersionOut(
        source_id=rec["source_id"], version=rec["version"], spec=SourceSpecOut(**rec["spec"])
    )


@app.get("/sources/{source_id}", response_model=SourceVersionOut)
def read_source(source_id: str, request: Request) -> SourceVersionOut:
    rec = _service(request).get_source(source_id)
    return SourceVersionOut(
        source_id=rec["source_id"], version=rec["version"], spec=SourceSpecOut(**rec["spec"])
    )


@app.put("/sources/{source_id}", response_model=SourceVersionOut)
def update_source(source_id: str, payload: SourceIn, request: Request) -> SourceVersionOut:
    rec = _service(request).replace_source(source_id, payload.text)
    return SourceVersionOut(
        source_id=rec["source_id"], version=rec["version"], spec=SourceSpecOut(**rec["spec"])
    )


@app.post("/rules/validate", response_model=list[RuleValidationOut])
def validate_rules(rules: list[RuleIn], request: Request) -> list[RuleValidationOut]:
    return _service(request).validate_rules(rules)


@app.post("/plans", response_model=PlanSummary | PlanDetail, status_code=201)
def create_plan(payload: PlanRequest, request: Request, response: Response):
    diag = _diag(request)
    result, run_id = _service(request).create_plan(payload, diag)
    response.headers["X-Run-Id"] = run_id
    return result


@app.get("/plans/{plan_id}", response_model=PlanDetail)
def read_plan(plan_id: str, request: Request) -> PlanDetail:
    detail, _plan, _text, _ver = _service(request).get_plan_detail(plan_id)
    return detail


def _stream_text(chunks: Iterator[str]) -> Iterator[bytes]:
    for ch in chunks:
        yield ch.encode("utf-8")


@app.post("/plans/{plan_id}/apply")
def apply_plan(plan_id: str, request: Request) -> StreamingResponse:
    """流式应用：返回 ``text/plain`` UTF-8 分片；版本登记在流末尾提交。"""
    diag = _diag(request)
    generator, meta, run_id = _service(request).apply_plan_stream(plan_id, diag)
    headers = {
        "X-Run-Id": run_id,
        "X-Source-Id": meta["source_id"],
        "X-Source-Version": str(meta["source_version"]),
        "X-Replaced-Count": str(meta["replaced"]),
    }
    return StreamingResponse(
        _stream_text(generator()),
        media_type="text/plain; charset=utf-8",
        headers=headers,
    )


@app.post("/plans/{plan_id}/apply/collect", response_model=ApplyResult)
def apply_plan_collect(plan_id: str, request: Request, response: Response) -> ApplyResult:
    """非流式：应用并返回新版本元数据（正文可用源版本接口读取）。"""
    diag = _diag(request)
    result, run_id = _service(request).apply_plan_collect(plan_id, diag)
    response.headers["X-Run-Id"] = run_id
    return result


@app.get("/diagnostics/{run_id}", response_model=DiagnosticsOut)
def read_diag(run_id: str, request: Request) -> DiagnosticsOut:
    events = request.app.state.repo.get_diag(run_id)
    return DiagnosticsOut(run_id=run_id, events=events)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
