"""FastAPI HTTP 接口（validation API）。

端点：
* POST /register                 扫描目录、读取 Parquet 统计、事务化注册
* POST /plan                     仅执行两级裁剪，返回带原因码的决策
* POST /validate                 裁剪 + PyArrow 全文件逐行扫描，对比零漏行
* GET  /tables                   已注册表
* GET  /tables/{name}            表元数据
* GET  /requests/{request_id}    裁剪审计（决策、版本、不确定性）
* GET  /healthz / GET /versions
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .config import Config
from .logging_config import event, get_logger
from .schemas import PlanIn, RegisterIn, ValidateIn
from .service import PruningService
from .versions import version_bundle

_LOG = get_logger("pruning.api")


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config()
    app = FastAPI(title="Two-level Pruning Backend", version="1.0.0")
    app.state.config = config
    app.state.service = PruningService(config)
    svc = app.state.service

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "versions": version_bundle()}

    @app.get("/versions")
    def versions():
        return version_bundle()

    @app.post("/register")
    def register(req: RegisterIn):
        try:
            return svc.register(req)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/plan")
    def plan(req: PlanIn):
        try:
            return svc.plan_only(req)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/validate")
    def validate(req: ValidateIn):
        try:
            return svc.validate(req)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/tables")
    def list_tables():
        return {"tables": svc.catalog.list_tables()}

    @app.get("/tables/{name}")
    def get_table(name: str):
        from .model import model_to_dict
        try:
            md = svc.catalog.load_table(name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        d = model_to_dict(md)
        return d

    @app.get("/requests/{request_id}")
    def get_request(request_id: str):
        audit = svc.catalog.get_audit(request_id)
        if audit is None:
            raise HTTPException(status_code=404, detail="request 未找到")
        return audit

    return app


def run():  # console script entrypoint
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=8000)


# 供 `uvicorn pruning.api:app` 使用
app = create_app()
