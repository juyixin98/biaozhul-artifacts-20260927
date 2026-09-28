"""FastAPI 验证接口。

端点:
  GET  /health
  GET  /datasets
  POST /datasets/register                 登记数据集目录
  POST /audit                             执行审计 (原子落库)
  GET  /audit/{run_id}                    取审计报告 (裁决 + 脱敏诊断)
  POST /query                             受信统计剪枝查询 + 全扫正确性对照
  GET  /datasets/{name}/diagnostics       最近审计的诊断事件流

每个请求都带 request_id (响应体与 X-Request-ID 头), 诊断事件关联该 id。
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import adapter, audit, query as query_mod
from .catalog import Catalog
from .config import Settings
from .logical import LogicalType
from .prune import Predicate


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    name: str
    root: str


class AuditRequest(BaseModel):
    dataset: str
    request_id: str | None = None
    mask_sensitive: bool | None = None


class QueryRequest(BaseModel):
    dataset: str
    column: str
    op: Literal["eq", "ne", "lt", "le", "gt", "ge", "is_null", "not_null"]
    value: Any = None
    run_id: str | None = None  # 默认使用该数据集最近一次 ok 审计
    request_id: str | None = None
    limit: int | None = Field(default=None, ge=1)
    redact: bool = True


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------
class State(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    settings: Settings
    catalog: Catalog


def _json_default(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"不可序列化: {type(value)}")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load().ensure_dirs()
    settings.ensure_dirs()
    catalog = Catalog(settings.effective_db_path)
    app = FastAPI(
        title="colaudit 列式统计审计后端",
        version="0.1.0",
    )
    app.state.colaudit = State(settings=settings, catalog=catalog)

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    def _load(name: str) -> adapter.Dataset:
        row = catalog.get_dataset(name)
        if row is None:
            raise HTTPException(404, f"未登记的数据集: {name}")
        try:
            return adapter.load_dataset(row["root"])
        except (FileNotFoundError, ValueError, KeyError) as exc:
            raise HTTPException(400, f"数据集加载失败: {exc}") from exc

    def _coerce_value(raw: Any, logical: LogicalType) -> Any:
        if raw is None:
            return None
        if isinstance(raw, str) and raw == "NaN" and logical is LogicalType.FLOAT:
            return float("nan")
        if logical is LogicalType.INT:
            return int(raw)
        if logical is LogicalType.FLOAT:
            return float(raw)
        if logical is LogicalType.BOOL:
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                return raw.strip().lower() in {"true", "1", "yes"}
            return bool(raw)
        if logical is LogicalType.DATE:
            if isinstance(raw, str):
                return _dt.date.fromisoformat(raw)
            raise HTTPException(400, "日期参数需为 ISO yyyy-mm-dd 字符串")
        return str(raw)

    @app.get("/health")
    def health(request: Request):
        return {
            "status": "ok",
            "request_id": request.state.request_id,
            "db": str(settings.effective_db_path),
        }

    @app.get("/datasets")
    def list_datasets():
        return {"datasets": catalog.list_datasets()}

    @app.post("/datasets/register")
    def register(body: RegisterRequest, request: Request):
        root = Path(body.root)
        if not root.is_absolute():
            root = (Path.cwd() / root).resolve()
        try:
            ds = adapter.load_dataset(root)
        except (FileNotFoundError, ValueError, KeyError) as exc:
            raise HTTPException(400, f"数据集无效: {exc}") from exc
        catalog.register_dataset(
            body.name,
            root,
            columns=[
                {
                    "name": c.name,
                    "logical_type": c.logical_type.value,
                    "sensitive": c.sensitive,
                }
                for c in ds.columns
            ],
            sensitive=ds.sensitive_columns,
        )
        return {
            "registered": body.name,
            "root": str(root),
            "request_id": request.state.request_id,
            "columns": [
                {"name": c.name, "logical_type": c.logical_type.value,
                 "sensitive": c.sensitive}
                for c in ds.columns
            ],
        }

    @app.post("/audit")
    def run_audit(body: AuditRequest, request: Request):
        ds = _load(body.dataset)
        mask = (
            body.mask_sensitive
            if body.mask_sensitive is not None
            else settings.mask_sensitive
        )
        report = audit.audit_dataset(
            ds,
            request_id=body.request_id or request.state.request_id,
            mask_sensitive=mask,
        )
        # 报告以登记名为数据集键, 保持与 datasets 表外键一致
        report["dataset"] = body.dataset
        try:
            catalog.save_report(report)
        except Exception as exc:  # 事务回滚, 报告不落库
            raise HTTPException(
                400, f"审计结果事务写入失败 (已回滚): {exc}"
            ) from exc
        return JSONResponse(
            _jsonable({k: report[k] for k in
                       ("run_id", "request_id", "dataset", "summary")}),
            headers={"X-Request-ID": report["request_id"]},
        )

    @app.get("/audit/{run_id}")
    def get_audit(run_id: str):
        report = catalog.load_report(run_id)
        if report is None:
            raise HTTPException(404, f"无此审计运行: {run_id}")
        return _jsonable(report)

    @app.post("/query")
    def run_query(body: QueryRequest, request: Request):
        ds = _load(body.dataset)
        logical = ds.column(body.column).logical_type
        value = _coerce_value(body.value, logical)
        pred = Predicate(column=body.column, op=body.op, value=value)

        run_id = body.run_id or catalog.latest_run_id(body.dataset)
        if run_id is None:
            raise HTTPException(
                409,
                "该数据集尚无成功审计; 请先 POST /audit 再查询",
            )
        report = catalog.load_report(run_id)
        if report is None:
            raise HTTPException(404, f"审计运行不存在: {run_id}")
        page_verdicts = _index_page_verdicts(report["verdicts"])

        rid = body.request_id or request.state.request_id
        result = query_mod.execute(
            ds,
            pred,
            page_verdicts,
            request_id=rid,
            limit=body.limit,
            redact=body.redact,
        )
        # 正确性对照: 同一谓词的无统计全扫基线
        total, matched = query_mod.full_scan_counts(ds, pred)
        correct = (
            result.total_rows == total and result.matched_rows == matched
        )
        payload = result.to_dict()
        payload["baseline_full_scan"] = {
            "total_rows": total,
            "matched_rows": matched,
        }
        payload["correctness"] = {
            "matches_baseline": correct,
            "note": "剪枝查询命中集合必须等于无统计全扫; false 即为缺陷",
        }
        payload["run_id"] = run_id
        return _jsonable(payload)

    @app.get("/datasets/{name}/diagnostics")
    def dataset_diagnostics(name: str, limit: int = 200):
        run_id = catalog.latest_run_id(name)
        if run_id is None:
            raise HTTPException(404, f"数据集 {name} 无审计运行")
        report = catalog.load_report(run_id)
        return {
            "dataset": name,
            "run_id": run_id,
            "diagnostics": report["diagnostics"][:limit],
        }

    return app


def _index_page_verdicts(
    rows: list[dict[str, Any]],
) -> dict[tuple, dict[str, dict[str, Any]]]:
    out: dict[tuple, dict[str, dict[str, Any]]] = {}
    for r in rows:
        if r["scope"] != "page":
            continue
        key = (r["file"], r["row_group"], r["page"])
        out.setdefault(key, {})[r["column_name"]] = r
    return out


def _jsonable(payload: Any) -> Any:
    return json.loads(json.dumps(payload, default=_json_default,
                                 ensure_ascii=False))
