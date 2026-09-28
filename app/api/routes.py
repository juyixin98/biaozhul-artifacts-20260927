"""HTTP 接口层。

路由
====
* ``GET  /health``                 —— 健康检查与版本
* ``POST /datasets``               —— 提交合成数据集（密文落盘）
* ``GET  /datasets``               —— 列出数据集（仅元数据/指纹）
* ``GET  /datasets/{id}``          —— 数据集元数据（不返回行数据）
* ``POST /analyze``                —— 提交即分析（真实输入→输出）
* ``POST /datasets/{id}/analyze``  —— 对已存数据集用阈值重放
* ``GET  /runs/{id}``              —— 取回不可变运行结果
* ``GET  /runs``                   —— 列出运行
* ``GET  /audit``                  —— 审计记录（脱敏明细）
* ``GET  /audit/verify``           —— 校验哈希链完整性
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app import __version__
from app.models import DatasetIn, RunResponse

router = APIRouter()


class AnalyzeRequest(DatasetIn):
    """直接分析的请求体即数据集本体。"""


class ReplayRequest(BaseModel):
    k: int = Field(default=2, ge=2)
    l: int = Field(default=1, ge=1)


@router.get("/health")
def health(request: Request) -> dict:
    state = request.app.state.app_state
    return {
        "status": "ok",
        "service": "anon-risk",
        "version": __version__,
        "key_source": state.service.crypto.key_source,
    }


@router.post("/datasets", status_code=201)
def submit_dataset(payload: DatasetIn, request: Request) -> dict:
    return request.app.state.app_state.service.submit_dataset(payload)


@router.get("/datasets")
def list_datasets(request: Request, limit: int = 100) -> dict:
    items = request.app.state.app_state.service.store.list_schemas(limit=min(limit, 500))
    return {"items": items, "count": len(items)}


@router.get("/datasets/{schema_id}")
def get_dataset(schema_id: str, request: Request) -> dict:
    meta = request.app.state.app_state.service.store.get_schema_meta(schema_id)
    return meta  # 只含元数据与指纹，不含行数据


@router.post("/analyze", response_model=RunResponse)
def analyze(payload: AnalyzeRequest, request: Request) -> RunResponse:
    # 先密文入库（状态隔离/可重放），再以存储身份分析，运行结果随之持久化
    svc = request.app.state.app_state.service
    meta = svc.submit_dataset(payload)
    return svc.run_stored(meta["schema_id"], payload.k, payload.l)


@router.post("/datasets/{schema_id}/analyze", response_model=RunResponse)
def analyze_stored(schema_id: str, payload: ReplayRequest, request: Request) -> RunResponse:
    return request.app.state.app_state.service.run_stored(schema_id, payload.k, payload.l)


@router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str, request: Request) -> dict:
    return request.app.state.app_state.service.store.get_run(run_id)


@router.get("/runs")
def list_runs(request: Request, schema_id: str | None = None, limit: int = 100) -> dict:
    items = request.app.state.app_state.service.store.list_runs(
        schema_id=schema_id, limit=min(limit, 500)
    )
    return {"items": items, "count": len(items)}


@router.get("/audit")
def read_audit(request: Request, limit: int = 100) -> dict:
    records = request.app.state.app_state.audit.read_all()
    records = records[-min(limit, 1000) :]
    return {"items": records, "count": len(records)}


@router.get("/audit/verify")
def verify_audit(request: Request) -> dict:
    return request.app.state.app_state.audit_verify()
