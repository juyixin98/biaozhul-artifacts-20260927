"""HTTP API.

Endpoints
---------
GET  /health
POST /abi/encode              {types:[...], values:[...]}      -> hex
POST /abi/decode              {types:[...], data:"0x.."}       -> values
POST /abi/selector            {name, types}                    -> 4-byte hex
POST /abi/encode-call         {name, types, values}            -> calldata hex
POST /replay                  run the offline scenario         -> report
GET  /runs, /runs/{id}, /transactions, /accounts, /account/{addr}, /events

Errors are returned as JSON ``{error:{code,message}}`` with the specific codec
category; nothing is collapsed into a generic success.
"""
from __future__ import annotations

import uuid
from typing import Any, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .abi import (
    ABIError,
    decode,
    encode,
    encode_call,
    function_selector,
    parse_type,
)
from .chain import ChainError
from .config import SETTINGS
from .log_utils import get_logger, set_run_id
from .replay import replay
from .storage import Repository

log = get_logger("api")

app = FastAPI(title="ABI Restricted Backend", version="1.0.0")
_repo: Optional[Repository] = None


def get_repo() -> Repository:
    global _repo
    if _repo is None:
        _repo = Repository(SETTINGS.db_path)
    return _repo


@app.middleware("http")
async def correlation(request: Request, call_next):
    run_id = request.headers.get("x-run-id") or f"req-{uuid.uuid4()}"
    set_run_id(run_id)
    response = await call_next(request)
    response.headers["x-run-id"] = run_id
    return response


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.exception_handler(ABIError)
async def _abi_error(request: Request, exc: ABIError):
    log.warning("abi error", extra={"verdict": "fail", "type": exc.error_code, "reason": str(exc)})
    return _err(400, exc.error_code, str(exc))


@app.exception_handler(ChainError)
async def _chain_error(request: Request, exc: ChainError):
    log.warning("chain error", extra={"verdict": "fail", "type": exc.error_code, "reason": str(exc)})
    return _err(422, exc.error_code, str(exc))


class EncodeBody(BaseModel):
    types: List[Any]
    values: List[Any]


class DecodeBody(BaseModel):
    types: List[Any]
    data: str = Field(description="0x-prefixed ABI blob")


class SelectorBody(BaseModel):
    name: str
    types: List[Any]


class CallBody(BaseModel):
    name: str
    types: List[Any]
    values: List[Any]


def _normalize_types(types: List[Any]):
    return [parse_type(t) if isinstance(t, str) else _json_type(t) for t in types]


def _json_type(component: dict):
    from .abi import type_from_json

    return type_from_json(component)


def _hex_to_bytes(s: str) -> bytes:
    try:
        if s.startswith("0x"):
            return bytes.fromhex(s[2:])
        return bytes.fromhex(s)
    except ValueError as exc:
        raise ABIError(f"invalid hex input: {exc}") from exc


def _present(header, value: Any) -> Any:
    """Render decoded values for JSON (bytes -> 0x hex, tuples -> lists)."""
    from .abi import (
        AddressType,
        BytesType,
        DynamicArrayType,
        FixedArrayType,
        FixedBytesType,
        StringType,
        TupleType,
    )

    if isinstance(header, AddressType):
        return hex(value)
    if isinstance(header, (BytesType, FixedBytesType)):
        return "0x" + bytes(value).hex()
    if isinstance(header, StringType):
        return value
    if isinstance(header, TupleType):
        return [_present(h, v) for h, v in zip(header.components, value)]
    if isinstance(header, (DynamicArrayType, FixedArrayType)):
        return [_present(header.element, v) for v in value]
    return value


@app.get("/health")
def health():
    return {"status": "ok", "service": "abibackend", "version": "1.0.0"}


@app.post("/abi/encode")
def abi_encode(body: EncodeBody):
    headers = _normalize_types(body.types)
    # Values arrive as JSON; addresses may be hex strings (encoder accepts both
    # int and 0x string). bytes values must be 0x strings.
    blob = encode(headers, body.values)
    return {"data": "0x" + blob.hex(), "length": len(blob)}


@app.post("/abi/decode")
def abi_decode(body: DecodeBody):
    headers = _normalize_types(body.types)
    blob = _hex_to_bytes(body.data)
    values = decode(headers, blob)
    presented = [_present(h, v) for h, v in zip(headers, values)]
    return {"values": presented}


@app.post("/abi/selector")
def abi_selector(body: SelectorBody):
    headers = _normalize_types(body.types)
    sel = function_selector(body.name, headers)
    signature = body.name + "(" + ",".join(h.canonical() for h in headers) + ")"
    return {"selector": "0x" + sel.hex(), "signature": signature}


@app.post("/abi/encode-call")
def abi_encode_call(body: CallBody):
    headers = _normalize_types(body.types)
    blob = encode_call(body.name, headers, body.values)
    return {"data": "0x" + blob.hex(), "length": len(blob)}


@app.post("/replay")
def run_replay():
    report = replay(get_repo(), mode="api")
    return report.to_dict()


@app.get("/runs")
def runs(limit: int = 50):
    return {"runs": get_repo().list_runs(limit)}


@app.get("/runs/{run_id}")
def one_run(run_id: str):
    run = get_repo().get_run(run_id)
    if run is None:
        return _err(404, "not_found", f"no run {run_id}")
    return run


@app.get("/transactions")
def transactions(run_id: Optional[str] = None, limit: int = 100):
    return {"transactions": get_repo().list_transactions(run_id, limit)}


@app.get("/accounts")
def accounts():
    return {"accounts": get_repo().list_accounts()}


@app.get("/account/{address}")
def account(address: str):
    acct = get_repo().get_account(address)
    if acct is None:
        return _err(404, "not_found", f"no account {address}")
    return acct


@app.get("/events")
def events(limit: int = 100):
    return {"events": get_repo().list_events(limit)}
