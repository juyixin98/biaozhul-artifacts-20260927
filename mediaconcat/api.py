"""FastAPI 应用：提交拼接规划作业、查询状态与逐样本计划、校验报告。"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from . import __version__
from .api_models import ConcatRequest, JobDetail, JobSummary
from .config import settings
from .config import Settings
from .jobstore import JobStore
from .logging_setup import bind, configure_logging, run_id
from .service import PlanningService

LOG = configure_logging(settings.log_dir, settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时再读一次环境，允许测试/部署覆盖（DB、夹具目录、日志目录等）
    app_settings = Settings.from_env()
    configure_logging(app_settings.log_dir, app_settings.log_level)
    store = JobStore(app_settings.db_path, run_id())
    app.state.store = store
    app.state.service = PlanningService(store, app_settings, bind())
    bind().step("startup", "ready", version=__version__, db=app_settings.db_path,
                fixtures=app_settings.fixtures_dir, allow_ffprobe=app_settings.allow_ffprobe)
    yield
    store.close()


app = FastAPI(
    title="媒体拼接样本边界规划 API",
    version=__version__,
    description="无重编码拼接可行性分析与逐样本计划（本地合成夹具驱动）",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__, "run_id": run_id()}


@app.post("/jobs", status_code=201)
def create_job(req: ConcatRequest) -> dict:
    if req.cuts and len(req.cuts) > len(req.sources):
        raise HTTPException(422, "cuts 数量不能超过 sources")
    service: PlanningService = app.state.service
    cuts = [c.model_dump() for c in req.cuts] if req.cuts else None
    job_id = service.submit(req.sources, req.output_container, cuts=cuts,
                            job_id=req.job_id)
    row = app.state.store.get(job_id)
    return {
        "job_id": job_id,
        "status": row["status"],
        # failed 也如实返回，并给出可查询的错误端点
        "detail": f"/jobs/{job_id}",
    }


@app.get("/jobs", response_model=list[JobSummary])
def list_jobs(limit: int = 50) -> list[dict]:
    return app.state.store.list_jobs(limit)


@app.get("/jobs/{job_id}", response_model=JobDetail)
def get_job(job_id: str) -> dict:
    row = app.state.store.get(job_id)
    if not row:
        raise HTTPException(404, f"作业 {job_id} 不存在")
    detail = {
        "job_id": row["job_id"],
        "run_id": row["run_id"],
        "status": row["status"],
        "container": row["container"],
        "sources": json.loads(row["sources"]),
        "error_code": row["error_code"],
        "error": row["error"],
        "plan": json.loads(row["plan_json"]) if row["plan_json"] else None,
    }
    return detail


@app.get("/jobs/{job_id}/verify")
def verify_job(job_id: str) -> dict:
    """返回独立校验视图：按样本/按失败类别汇总，便于人工复核。"""
    row = app.state.store.get(job_id)
    if not row:
        raise HTTPException(404, f"作业 {job_id} 不存在")
    if row["status"] == "failed":
        return {
            "job_id": job_id,
            "status": "failed",
            "error_code": row["error_code"],
            "error": row["error"],
            "checks": [],
        }
    if not row["plan_json"]:
        return {"job_id": job_id, "status": row["status"], "checks": []}

    plan = json.loads(row["plan_json"])
    checks: list[dict] = []
    for seg in plan["segments"]:
        dts = [s["out_dts"] for s in seg["samples"]]
        checks.append({
            "clip_index": seg["clip_index"],
            "stream": seg["stream"],
            "samples": len(seg["samples"]),
            "min_dts": min(dts) if dts else None,
            "nonnegative": all(d >= 0 for d in dts),
            "strict_increasing": all(b > a for a, b in zip(dts, dts[1:])),
            "preroll_count": seg["preroll_count"],
            "pad_count": seg["pad_count"],
            "roles": {
                role: sum(1 for s in seg["samples"] if s["role"] == role)
                for role in ("content", "preroll_reference", "silence_pad",
                             "drop_encoder_delay")
            },
        })
    return {
        "job_id": job_id,
        "status": row["status"],
        "feasible": plan["feasible"],
        "mode": plan["mode"],
        "output_time_base": plan["output_time_base"],
        "output_duration_sec": plan["output_duration_sec"],
        "errors": [
            {"code": f["code"], "severity": f["severity"],
             "stream": f.get("stream"), "clip_index": f.get("clip_index"),
             "message": f["message"], "evidence": f.get("evidence", {})}
            for f in plan["findings"] if f["severity"] == "error"
        ],
        "warnings": [
            {"code": f["code"], "stream": f.get("stream"),
             "clip_index": f.get("clip_index"), "message": f["message"]}
            for f in plan["findings"] if f["severity"] == "warning"
        ],
        "checks": checks,
    }
