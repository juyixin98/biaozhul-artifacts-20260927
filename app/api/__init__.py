"""FastAPI HTTP 层。

端点：
  POST /abi/encode          按类型编码值序列
  POST /abi/decode          按类型解码十六进制 blob
  POST /abi/encode-call     按函数签名编码 calldata
  POST /abi/selector        计算函数选择器
  GET  /chain/state         查看内核账户状态
  POST /chain/block         提交一批原始 calldata(hex)，落块并持久化
  POST /chain/transact      提交一笔 {signature, args}，便捷封装
  GET  /chain/tx/{tx_hash}  索引查询回执
  GET  /healthz             健康/版本

所有失败返回结构化错误信封 {ok:false,error:{category,message}}，
带 HTTP 4xx；绝不把异常返回成成功。每个请求写 runlog，可关联 run_id。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..abi import (
    ABIError,
    decode,
    encode,
    encode_call,
    function_selector,
)
from ..abi.hashing import keccak256
from ..kernel import METHOD_ABIS, ChainKernel
from ..storage import Storage
from .. import __version__
from ..config import settings
from ..runlog import get_logger, run_id


class EncodeReq(BaseModel):
    types: list[str]
    values: list


class DecodeReq(BaseModel):
    types: list[str]
    data: str = Field(description="0x 前缀十六进制 blob")


class SelectorReq(BaseModel):
    signature: str


class CallReq(BaseModel):
    signature: str
    args: list


class BlockReq(BaseModel):
    calldata: list[str] = Field(description="0x 前缀的原始 calldata 列表")


def _hex_bytes(s: str) -> bytes:
    raw = s[2:] if s.startswith("0x") else s
    try:
        return bytes.fromhex(raw)
    except ValueError as e:
        raise _HttpError("invalid_hex", f"不是合法十六进制: {e}") from e


def _materialize(v):
    """把 JSON 调用参数物化为 ABI 可编码的 Python 值。

    支持 {"bytes": "0x.."} 标签；list/tuple 递归；其余原样。
    """
    if isinstance(v, dict) and set(v.keys()) == {"bytes"}:
        return _hex_bytes(v["bytes"])
    if isinstance(v, list):
        return [_materialize(x) for x in v]
    return v


class _HttpError(Exception):
    def __init__(self, category: str, message: str, status: int = 400):
        super().__init__(message)
        self.category = category
        self.message = message
        self.status = status


# JSON 中 bytes 用 0x hex、大整数用字符串（前端安全）
def _jsonable(v):
    if isinstance(v, bytes):
        return "0x" + v.hex()
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, int) and (v > 2 ** 53 or v < -(2 ** 53)):
        return str(v)
    return v


_state: dict | None = None
logger = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _state, logger
    logger = get_logger("api")
    storage = Storage(settings.db_path)
    kernel = ChainKernel()
    _state = {"kernel": kernel, "storage": storage, "logger": logger}
    logger.step("api_startup", identity={"db": settings.db_path},
                host=settings.host, port=settings.port)
    try:
        yield
    finally:
        summary = storage.stats()
        storage.close()
        logger.close(summary=summary)


app = FastAPI(
    title="受限以太坊 ABI 编解码后端",
    version=__version__,
    lifespan=lifespan,
)


@app.exception_handler(_HttpError)
async def http_error_handler(request: Request, exc: _HttpError):
    return JSONResponse(
        status_code=exc.status,
        content={"ok": False, "error": {"category": exc.category,
                                        "message": exc.message}},
    )


@app.exception_handler(ABIError)
async def abi_error_handler(request: Request, exc: ABIError):
    # 已知 ABI 失败类别 → 400 + 稳定 category，绝不当成功
    return JSONResponse(
        status_code=400,
        content={"ok": False, "error": {"category": exc.category,
                                        "message": str(exc)}},
    )


@app.middleware("http")
async def request_logging(request: Request, call_next):
    """逐请求结构化日志：关联方法/路径/请求体指纹/运行身份/判定与失败类别。"""
    body_fingerprint = None
    if request.method == "POST":
        raw = await request.body()
        body_fingerprint = {
            "bytes": len(raw),
            # 记录前 128 字节预览 + keccak 指纹，既能关联输入又不刷屏
            "sha": "0x" + keccak256(raw).hex()[:16],
            "preview": raw[:128].decode("utf-8", "replace"),
        }

        async def receive():
            return {"type": "http.request", "body": raw}

        request = Request(request.scope, receive)

    response = await call_next(request)
    response.headers["x-run-id"] = run_id()

    identity = {"method": request.method, "path": request.url.path,
                "run_id": run_id()}
    if response.status_code >= 400:
        # 读取响应体取出稳定 category，保证失败不被记成成功
        resp_body = b"".join([chunk async for chunk in response.body_iterator])

        async def consume():
            return {"type": "http.response.body", "body": resp_body}

        import json as _json
        category = "http_" + str(response.status_code)
        try:
            category = _json.loads(resp_body).get("error", {}).get("category", category)
        except Exception:
            pass
        if logger is not None:
            logger.failure(
                "http_request", category=category,
                message=f"{request.method} {request.url.path} -> {response.status_code}",
                identity=identity, request=body_fingerprint,
            )
        return _ResponseWithBody(response, resp_body)

    if logger is not None:
        logger.step("http_request", identity=identity,
                    request=body_fingerprint, status=response.status_code)
    return response


from starlette.responses import Response as _StarletteResponse  # noqa: E402


class _ResponseWithBody(_StarletteResponse):
    """把已读取的响应体重新包装返回。"""
    def __init__(self, base, body: bytes):
        super().__init__(content=body, status_code=base.status_code,
                         headers=dict(base.headers), media_type=base.media_type)


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__, "run_id": run_id()}


@app.post("/abi/encode")
def do_encode(req: EncodeReq):
    values = [_materialize(v) for v in req.values]
    blob = encode(req.types, tuple(values))
    return {"ok": True, "data": "0x" + blob.hex(), "length": len(blob)}


@app.post("/abi/decode")
def do_decode(req: DecodeReq):
    blob = _hex_bytes(req.data)
    values = decode(req.types, blob)
    return {"ok": True, "values": [_jsonable(v) for v in values]}


@app.post("/abi/selector")
def do_selector(req: SelectorReq):
    sel = function_selector(req.signature)
    return {"ok": True, "signature": req.signature, "selector": "0x" + sel.hex()}


@app.post("/abi/encode-call")
def do_encode_call(req: CallReq):
    args = [_materialize(a) for a in req.args]
    blob = encode_call(req.signature, tuple(args))
    return {"ok": True, "calldata": "0x" + blob.hex(), "length": len(blob)}


@app.get("/chain/methods")
def list_methods():
    return {"ok": True, "methods": [
        {"signature": sig, "types": list(types)}
        for sig, types in METHOD_ABIS.values()
    ]}


@app.get("/chain/state")
def chain_state(address: str | None = None):
    kernel: ChainKernel = _state["kernel"]
    if address:
        addr = _hex_bytes(address)
        acct = kernel.account(addr)
        return {"ok": True, "account": {
            "address": address, "balance": str(acct.balance),
            "nonce": acct.nonce, "note": acct.note,
        }}
    return {
        "ok": True,
        "block_number": kernel.block_number,
        "state_root": "0x" + kernel.compute_state_root().hex(),
        "accounts": {
            "0x" + a.hex(): {"balance": str(x.balance), "nonce": x.nonce,
                             "note": x.note}
            for a, x in sorted(kernel.accounts.items())
        },
    }


@app.post("/chain/transact")
def chain_transact(req: CallReq):
    args = [_materialize(a) for a in req.args]
    calldata = encode_call(req.signature, tuple(args))
    return _apply_one(calldata)


@app.post("/chain/block")
def chain_block(req: BlockReq):
    kernel: ChainKernel = _state["kernel"]
    storage: Storage = _state["storage"]
    calldatas = [_hex_bytes(c) for c in req.calldata]
    block = kernel.apply_block(calldatas)
    storage.save_block(block, calldatas, kernel.accounts)
    return {
        "ok": True,
        "block_number": block.number,
        "block_hash": "0x" + block.block_hash.hex(),
        "state_root": "0x" + block.state_root.hex(),
        "receipts": [_receipt_json(r) for r in block.receipts],
    }


def _apply_one(calldata: bytes):
    kernel: ChainKernel = _state["kernel"]
    storage: Storage = _state["storage"]
    # 单笔即一个区块：落块、持久化，回执体现成功或明确回滚类别。
    block = kernel.apply_block([calldata])
    storage.save_block(block, [calldata], kernel.accounts)
    receipt = block.receipts[0]
    return {
        "ok": receipt.status == "ok",
        "block_number": block.number,
        "block_hash": "0x" + block.block_hash.hex(),
        "state_root": "0x" + block.state_root.hex(),
        "receipt": _receipt_json(receipt),
    }


def _receipt_json(r) -> dict:
    return {
        "tx_hash": r.tx_hash,
        "block_number": r.block_number,
        "index": r.index,
        "signature": r.signature,
        "status": r.status,
        "error_category": r.error_category,
        "error_message": r.error_message,
        "from_account": r.from_account,
        "to_account": r.to_account,
        "amount": str(r.amount) if r.amount is not None else None,
    }


@app.get("/chain/tx/{tx_hash}")
def get_tx(tx_hash: str):
    storage: Storage = _state["storage"]
    row = storage.get_transaction(tx_hash)
    if row is None:
        raise _HttpError("not_found", f"交易 {tx_hash} 不存在", status=404)
    return {"ok": True, "transaction": row}
