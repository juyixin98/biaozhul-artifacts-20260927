"""FastAPI 服务：把内核暴露为本地 HTTP 接口。

所有失败都是业务判定（200 + accepted=false + 失败分类），只有请求体无法
解析为 JSON 才是协议层错误。任何拒绝路径都不触发状态变更。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..chain import ChainKernel
from ..config import load_settings
from ..replay import replay_bundle

settings = load_settings()
kernel = ChainKernel(settings)

app = FastAPI(
    title="RSV - restricted stack-script verifier",
    version="0.1.0",
    description="本地测试交易的受限栈脚本验证（非完整链）",
)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "network": settings.chain.network,
        "default_domain": settings.chain.default_domain,
        "limits": settings.limits.__dict__,
        "state_root": kernel.store.state_root(),
    }


@app.post("/bootstrap")
async def bootstrap(req: Request) -> JSONResponse:
    genesis = await _json_body(req)
    if isinstance(genesis, JSONResponse):
        return genesis
    try:
        result = kernel.bootstrap_from_dict(genesis)
        return JSONResponse({"accepted": True, **result})
    except Exception as exc:  # bootstrap 冲突等业务失败
        return _exc_response(exc)


@app.post("/tx/verify")
async def tx_verify(req: Request) -> JSONResponse:
    tx = await _json_body(req)
    if isinstance(tx, JSONResponse):
        return tx
    try:
        rep = kernel.verify_transaction_dict(tx, persist=False, kind="verify")
        return JSONResponse(rep.to_dict())
    except Exception as exc:  # 内核外的意外错误也要结构化
        return _exc_response(exc)


@app.post("/tx/submit")
async def tx_submit(req: Request) -> JSONResponse:
    tx = await _json_body(req)
    if isinstance(tx, JSONResponse):
        return tx
    try:
        rep = kernel.verify_transaction_dict(tx, persist=True, kind="submit")
        return JSONResponse(rep.to_dict())
    except Exception as exc:
        return _exc_response(exc)


@app.get("/utxos")
def utxos(domain: str | None = None) -> dict:
    rows = kernel.store.list_utxos(domain)
    return {
        "count": len(rows),
        "state_root": kernel.store.state_root(),
        "utxos": [
            {
                "txid": u.txid,
                "index": u.idx,
                "value": u.value,
                "domain": u.domain,
                "pubkey_script": u.pubkey_script,
            }
            for u in rows
        ],
    }


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> JSONResponse:
    rec = kernel.store.get_run(run_id)
    if rec is None:
        return JSONResponse(
            {"accepted": False, "failure": {"category": "state", "code": "state.run_not_found",
                                            "message": "run id 不存在"}},
            status_code=404,
        )
    return JSONResponse(
        {
            "run_id": rec.run_id,
            "ts": rec.ts,
            "kind": rec.kind,
            "accepted": rec.accepted,
            "txid": rec.txid,
            "category": rec.category,
            "code": rec.code,
            "detail": rec.detail,
            "message32": rec.message32,
            "trace": rec.trace,
            "reason": rec.reason,
        }
    )


@app.post("/replay")
async def replay(req: Request) -> JSONResponse:
    bundle = await _json_body(req)
    if isinstance(bundle, JSONResponse):
        return bundle
    try:
        res = replay_bundle(bundle)
        return JSONResponse(res.to_dict())
    except Exception as exc:
        return _exc_response(exc)


async def _json_body(req: Request):
    try:
        return await req.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            {
                "accepted": False,
                "failure": {
                    "category": "input",
                    "code": "input.malformed_tx",
                    "message": "请求体不是合法 JSON",
                    "detail": "",
                    "pc": None,
                },
            },
            status_code=400,
        )


def _exc_response(exc: Exception) -> JSONResponse:
    from ..errors import VerificationFailure

    if isinstance(exc, VerificationFailure):
        return JSONResponse(
            {"accepted": False, "txid": "", "failure": exc.to_dict(), "run_id": ""}
        )
    return JSONResponse(
        {
            "accepted": False,
            "failure": {
                "category": "compute",
                "code": "compute.internal",
                "message": "内部错误",
                "detail": repr(exc),
                "pc": None,
            },
        },
        status_code=500,
    )


def main() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)


if __name__ == "__main__":  # pragma: no cover
    main()
