"""FastAPI 应用。

接口：
- ``GET  /health``              版本与依赖信息；
- ``POST /jobs``                提交解析作业（同步执行，状态机全程落库）；
- ``GET  /jobs/{job_id}``       作业状态 + 日志；
- ``GET  /jobs/{job_id}/result``解析结果（失败作业返回错误类别，不伪装成功）；
- ``GET  /validate``            对照手写参考时间表的证据校验报告。
"""

from __future__ import annotations

import sys
from pathlib import Path

import fastapi
import numpy
import pydantic
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .. import __version__
from ..config import Settings, load_settings
from ..errors import JobNotFoundError
from ..jobs.runner import run_job
from ..jobs.store import JobStore
from ..validation.checks import run_validation

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REFERENCES_DIR = REPO_ROOT / "fixtures" / "references"


class JobRequest(BaseModel):
    path: str


def _job_view(store: JobStore, job_id: str) -> dict:
    row = store.get_or_raise(job_id)
    return {
        "job_id": row["job_id"],
        "run_id": row["run_id"],
        "status": row["status"],
        "progress": row["progress"],
        "input_path": row["input_path"],
        "input_sha256": row["input_sha256"],
        "error_class": row["error_class"],
        "error_detail": row["error_detail"],
        "logs": store.logs(job_id),
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.ensure_dirs()
    store = JobStore(settings.db_path)

    app = FastAPI(title="mp4-timeline", version=__version__)
    app.state.settings = settings
    app.state.store = store

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "service_version": __version__,
            "python": sys.version.split()[0],
            "dependencies": {
                "fastapi": fastapi.__version__,
                "numpy": numpy.__version__,
                "pydantic": pydantic.__version__,
            },
        }

    @app.post("/jobs", status_code=201)
    def submit_job(req: JobRequest) -> dict:
        path = Path(req.path).resolve()
        if not any(
            path == root or root in path.parents for root in settings.allowed_roots
        ):
            raise HTTPException(
                status_code=403,
                detail=f"路径不在允许范围内（allowed_roots）: {path}",
            )
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
        job_id = store.create(str(path))
        run_job(store, job_id, settings)  # 同步执行；状态迁移已落库
        return _job_view(store, job_id)

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        try:
            return _job_view(store, job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/jobs/{job_id}/result")
    def get_result(job_id: str) -> dict:
        try:
            row = store.get_or_raise(job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if row["status"] == "failed":
            # 失败如实上报，绝不返回成功载荷
            raise HTTPException(
                status_code=422,
                detail={
                    "status": "failed",
                    "error_class": row["error_class"],
                    "error_detail": row["error_detail"],
                },
            )
        if row["status"] != "done":
            raise HTTPException(
                status_code=409, detail=f"作业尚未完成: status={row['status']}"
            )
        return store.result(job_id)

    @app.get("/validate")
    def validate() -> dict:
        return run_validation(settings.fixtures_dir, REFERENCES_DIR)

    return app
