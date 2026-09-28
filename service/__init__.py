"""FastAPI 服务：仅暴露本地测试协议的验证/提交/查询接口。

- POST /transactions/verify ：只验证，绝不触碰状态；
- POST /transactions/submit：验证通过才原子提交，失败只返回分类；
- GET  /utxos、/transactions、/journal、/state：只读查询；
- 任何验证失败都返回结构化 {accepted:false, kind, code, detail, ...}，
  且输入错误使用 4xx、资源/计算/状态失败使用 422（区别于服务端 5xx）。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from chain import ChainKernel
from chain.replay import replay
from chain.store import Store
from stackvm.config import Settings, load_settings
from stackvm.errors import FailCode, VmFailure, kind_of
from stackvm.runlog import RunLogger
from stackvm.transaction import transaction_from_dict, txid_of


class TxInModel(BaseModel):
    txid: str = Field(min_length=64, max_length=64)
    vout: int = Field(ge=0)
    unlock: str = ""


class TxOutModel(BaseModel):
    value: int = Field(ge=0)
    script: str


class TxModel(BaseModel):
    version: int = 1
    locktime: int = 0
    inputs: list[TxInModel] = Field(min_length=1)
    outputs: list[TxOutModel] = Field(min_length=1)


def _fail_body(code: FailCode, detail: str, run_id: str | None = None,
               http_status: int | None = None) -> JSONResponse:
    body = {"accepted": False, "kind": kind_of(code).value,
            "code": code.value, "detail": detail}
    if run_id:
        body["run_id"] = run_id
    status = http_status or (400 if kind_of(code).value == "INPUT" else 422)
    return JSONResponse(status_code=status, content=body)


def create_app(settings: Settings | None = None, *, db_path: str | Path | None = None,
               bootstrap: bool = True, runlog_kind: str = "service") -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="StackVM 受限栈脚本验证", version="1.0.0")
    app.state.settings = settings
    app.state.runlog_kind = runlog_kind

    actual_db = Path(db_path) if db_path else settings.abspath(settings.storage.db_path)
    store = Store(actual_db)
    app.state.store = store
    app.state.kernel = ChainKernel(store, settings)

    if bootstrap and store.chain_height == 0:
        genesis_path = settings.abspath(settings.storage.genesis_fixture)
        if genesis_path.exists():
            genesis = json.loads(genesis_path.read_text(encoding="utf-8"))
            store.bootstrap_genesis(genesis, settings.chain.mint_total_cap)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):  # noqa: ANN202
        # VmFailure 已由各路径自行处理；这里兜底，绝不向客户端泄漏堆栈
        return JSONResponse(status_code=500, content={
            "accepted": False, "kind": "COMPUTE", "code": "INTERNAL_ERROR",
            "detail": f"服务内部错误: {type(exc).__name__}"})

    @app.exception_handler(RequestValidationError)
    async def _bad_request(request: Request, exc: RequestValidationError):  # noqa: ANN202
        return _fail_body(FailCode.REQUEST_MALFORMED,
                          f"请求结构不符合交易模式: {exc.errors()}", http_status=400)

    def _record(report_dict: dict, raw_tx: dict, run_id: str) -> None:
        logger = RunLogger(run_id, kind=app.state.runlog_kind,
                           log_dir=settings.abspath(settings.storage.runlog_dir))
        logger.event("request", tx=raw_tx)
        logger.event("verdict", **{k: report_dict.get(k) for k in (
            "accepted", "kind", "code", "detail", "txid", "digest",
            "total_in", "total_out")})
        for inp in report_dict.get("inputs", []):
            logger.event("input_eval", **{
                k: inp[k] for k in ("index", "prevout", "ok", "code",
                                    "detail", "budget_left")})
            for step in inp.get("trace", []):
                logger.event("trace_step", input=inp["index"], **step)
            for chk in inp.get("checks", []):
                logger.event("crypto_check", input=inp["index"], **chk)
        logger.set_summary({"verdict": report_dict.get("code"),
                            "accepted": report_dict.get("accepted")})
        logger.flush()

    @app.get("/health")
    def health():
        return {"status": "ok", "height": store.chain_height,
                "state_root": store.state_root, "db": str(actual_db)}

    @app.post("/transactions/verify")
    def verify_tx(tx_model: TxModel):
        run_logger = RunLogger(kind=app.state.runlog_kind,
                               log_dir=settings.abspath(settings.storage.runlog_dir))
        try:
            tx = transaction_from_dict(tx_model.model_dump())
        except VmFailure as fail:
            return _fail_body(fail.code, fail.detail)
        report = app.state.kernel.evaluate(tx)
        body = report.to_dict()
        body["run_id"] = run_logger.run_id
        _record(body, tx_model.model_dump(), run_logger.run_id)
        if not report.accepted:
            return _fail_body(report.code, report.detail, run_logger.run_id)
        body["accepted"] = True
        return JSONResponse(status_code=200, content=body)

    @app.post("/transactions/submit")
    def submit_tx(tx_model: TxModel):
        run_logger = RunLogger(kind=app.state.runlog_kind,
                               log_dir=settings.abspath(settings.storage.runlog_dir))
        try:
            tx = transaction_from_dict(tx_model.model_dump())
        except VmFailure as fail:
            return _fail_body(fail.code, fail.detail)
        report = app.state.kernel.submit(tx)
        body = report.to_dict()
        body["run_id"] = run_logger.run_id
        _record(body, tx_model.model_dump(), run_logger.run_id)
        if not report.accepted:
            # 验证失败：保证没有发生转账（submit 内部未进入 apply_transaction）
            return _fail_body(report.code, report.detail, run_logger.run_id)
        body["accepted"] = True
        body["state_root"] = store.state_root
        body["height"] = store.chain_height
        return JSONResponse(status_code=201, content=body)

    @app.get("/utxos")
    def utxos():
        return {"state_root": store.state_root, "utxos": store.list_utxos()}

    @app.get("/transactions")
    def transactions():
        rows = store.conn.execute(
            "SELECT txid,height,body,accepted_at FROM transactions ORDER BY height"
        ).fetchall()
        return {"transactions": [dict(r) for r in rows]}

    @app.get("/journal")
    def journal():
        store.verify_journal()
        return {"journal": store.list_journal()}

    @app.get("/state")
    def state():
        store.verify_journal()
        return {"height": store.chain_height, "state_root": store.state_root,
                "replay": replay(actual_db).to_dict()}

    return app


import os

if os.environ.get("STACKVM_DISABLE_AUTO_APP") == "1":
    # 测试场景：只使用 create_app() 工厂，避免导入即建库/引导
    app = None
else:
    app = create_app()
