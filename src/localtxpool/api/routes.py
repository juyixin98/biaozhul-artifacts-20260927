"""HTTP 路由。所有响应统一信封 ``{ok, data, request_id, warnings}``。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from ..encoding import hex_to_bytes, to_checksum_address
from ..errors import NotFound
from ..core import (
    PENDING, QUEUED, INCLUDED, MINED, EXPIRED, REPLACED, EVICTED,
)
from .schemas import (
    AccountOut, ConfirmIn, FundIn, JournalOut, PoolStatusOut, ProposeIn,
    RawTxIn, RollbackIn, TxOut,
)

router = APIRouter(tags=["txpool"])


def svc(request: Request):
    return request.app.state.service


def rid(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def ok(request: Request, data, warnings: list[str] | None = None,
       status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"ok": True, "data": data, "request_id": rid(request),
                 "warnings": warnings or []},
    )


def _tx_out(t) -> dict:
    return TxOut(
        tx_hash=t.tx_hash,
        sender=to_checksum_address(bytes.fromhex(t.sender[2:])),
        to=(to_checksum_address(bytes.fromhex(t.to_addr[2:])) if t.to_addr else None),
        nonce=t.nonce,
        gas_price=str(t.gas_price),
        gas_limit=t.gas_limit,
        value=str(t.value),
        data="0x" + t.data.hex(),
        received_at=t.received_at,
        expires_at=t.expires_at,
        status=t.status,
        reason=t.reason,
        reason_detail=t.reason_detail,
        replaced_by=t.replaced_by,
        block_number=t.block_number,
        position=t.position,
    ).model_dump()


# --------------------------------------------------------------------------- #
@router.get("/health")
def health(request: Request):
    s = svc(request)
    return ok(request, {
        "status": "ok", "version": request.app.version,
        "time": s.clock.now(), "chain_id": s.config.chain.chain_id,
        "storage": s.config.storage.path,
    })


# ---- 账户（合成夹具） ---- #
@router.post("/admin/fund")
def fund(request: Request, body: FundIn):
    s = svc(request)
    addr = to_checksum_address(hex_to_bytes(body.address, name="address")).lower()
    with s.repo.transaction():
        s.repo.ensure_account(addr, s.clock.now())
        delta = int(body.amount)
        new_balance = s.repo.adjust_balance(addr, delta, s.clock.now())
        if body.nonce is not None:
            row = s.repo.get_account(addr)
            s.repo.set_account(addr, int(row["balance"]), body.nonce, s.clock.now())
        # 余额变化后重新分类该发送者
        reclass = s.pool.classify_sender(addr, request_id=rid(request),
                                         reason="BALANCE_CHANGED")
        s.repo.add_journal(ts=s.clock.now(), request_id=rid(request),
                           action="fund", sender=addr, reason="BALANCE_CHANGED",
                           detail={"delta": str(delta), "new_balance": str(new_balance),
                                   "nonce_override": body.nonce})
    row = s.repo.get_account(addr)
    return ok(request, {"account": AccountOut(
        address=to_checksum_address(bytes.fromhex(addr[2:])),
        balance=row["balance"], nonce=row["nonce"]).model_dump(),
        "classification": {"pending": reclass["pending"],
                           "queued": [h for h, _ in reclass["queued"]]}})


@router.get("/accounts")
def list_accounts(request: Request):
    s = svc(request)
    rows = s.repo.all_accounts()
    return ok(request, [AccountOut(
        address=to_checksum_address(bytes.fromhex(r["address"][2:])),
        balance=r["balance"], nonce=r["nonce"]).model_dump() for r in rows])


@router.get("/accounts/{address}")
def get_account(request: Request, address: str):
    s = svc(request)
    row = s.repo.get_account(address.lower())
    if row is None:
        raise NotFound("unknown account", details={"address": address})
    return ok(request, AccountOut(
        address=to_checksum_address(bytes.fromhex(row["address"][2:])),
        balance=row["balance"], nonce=row["nonce"]).model_dump())


# ---- 交易 ---- #
@router.post("/transactions")
def submit_tx(request: Request, body: RawTxIn):
    s = svc(request)
    raw = hex_to_bytes(body.raw, name="raw")
    result = s.submit_raw(raw, request_id=rid(request))
    warnings = result.pop("warnings", [])
    return ok(request, result, warnings=warnings, status_code=202)


@router.get("/transactions/{tx_hash}")
def get_tx(request: Request, tx_hash: str):
    s = svc(request)
    t = s.repo.get_tx(tx_hash.lower())
    if t is None:
        raise NotFound("transaction not found", details={"tx_hash": tx_hash})
    return ok(request, _tx_out(t))


@router.get("/transactions")
def list_txs(
    request: Request,
    status: str = Query(default="pending,queued",
                        description="逗号分隔：pending|queued|included|mined|expired|replaced|evicted|all"),
    sender: str | None = None,
    limit: int = Query(default=200, le=2000),
):
    s = svc(request)
    statuses = ("pending", "queued", "included", "mined", "expired", "replaced", "evicted") \
        if status == "all" else tuple(x.strip() for x in status.split(",") if x.strip())
    if sender:
        txs = s.repo.list_sender(sender.lower(), statuses)
    else:
        txs = s.repo.list_all(statuses)
    txs = txs[:limit]
    return ok(request, [_tx_out(t) for t in txs])


@router.get("/pool/status")
def pool_status(request: Request):
    s = svc(request)
    r = s.repo
    proposed = r.latest_by_status("proposed")
    return ok(request, PoolStatusOut(
        head_number=s.chain.head_number(), head_hash=s.chain.head_hash(),
        proposed_block=proposed["number"] if proposed else None,
        pending=r.count_status((PENDING,)), queued=r.count_status((QUEUED,)),
        included=r.count_status((INCLUDED,)), expired=r.count_status((EXPIRED,)),
        replaced=r.count_status((REPLACED,)), evicted=r.count_status((EVICTED,)),
        mined=r.count_status((MINED,))).model_dump())


@router.post("/pool/reap-expired")
def reap(request: Request):
    s = svc(request)
    with s.repo.transaction():
        hashes = s.pool.reap_expired(request_id=rid(request))
    return ok(request, {"expired": hashes, "count": len(hashes)})


@router.get("/pool/candidate")
def candidate(request: Request, gas_limit: int | None = None):
    s = svc(request)
    return ok(request, s.chain.candidate_preview(gas_limit))


# ---- 区块 ---- #
@router.post("/blocks/propose")
def propose(request: Request, body: ProposeIn | None = None):
    body = body or ProposeIn()
    s = svc(request)
    result = s.chain.propose(gas_limit=body.gas_limit, coinbase=body.coinbase,
                             request_id=rid(request))
    return ok(request, result, status_code=201)


@router.post("/blocks/confirm")
def confirm(request: Request, body: ConfirmIn | None = None):
    body = body or ConfirmIn()
    s = svc(request)
    result = s.chain.confirm(body.block_number, request_id=rid(request))
    return ok(request, result)


@router.post("/blocks/discard")
def discard(request: Request, body: dict | None = None):
    s = svc(request)
    result = s.chain.discard(request_id=rid(request))
    return ok(request, result)


@router.post("/blocks/rollback")
def rollback(request: Request, body: RollbackIn | None = None):
    body = body or RollbackIn()
    s = svc(request)
    result = s.chain.rollback(body.n, request_id=rid(request))
    return ok(request, result)


@router.get("/blocks/head")
def head(request: Request):
    s = svc(request)
    head_row = s.repo.head_block()
    if head_row is None:
        return ok(request, {"number": 0, "hash": s.chain.head_hash(), "status": "genesis"})
    return ok(request, dict(head_row))


# ---- 可解释性 ---- #
@router.get("/explain/journals")
def journals(request: Request, tx_hash: str | None = None, request_id: str | None = None,
             block_number: int | None = None, sender: str | None = None,
             limit: int = Query(default=100, le=1000)):
    s = svc(request)
    rows = s.repo.query_journals(tx_hash=tx_hash, request_id=request_id,
                                 block_number=block_number, sender=sender, limit=limit)
    out = []
    for r in rows:
        d = dict(r)
        # detail 列存的是 JSON 字符串，透传并保证可解析
        try:
            d["detail"] = json.loads(d["detail"])
        except (TypeError, json.JSONDecodeError):
            d["detail"] = {"raw": d["detail"], "uncertain": True}
        out.append(JournalOut(**d).model_dump())
    return ok(request, out)
