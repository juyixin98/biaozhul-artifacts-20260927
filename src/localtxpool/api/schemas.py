"""Pydantic 请求/响应模型。

wei 金额在 JSON 中一律为十进制**字符串**（JS 安全整数只有 53 位），
时间为 Unix 秒整数，gas_limit/nonce 为普通整数。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# 请求
# --------------------------------------------------------------------------- #
class RawTxIn(BaseModel):
    raw: str = Field(description="0x 前缀的 RLP 已签名遗留交易")


class FundIn(BaseModel):
    address: str
    amount: str = Field(description="合成环境注入的 wei 金额（可负数，用于余额变化夹具）")
    nonce: int | None = Field(default=None, description="可选：直接设置链上 nonce")


class ProposeIn(BaseModel):
    gas_limit: int | None = None
    coinbase: str | None = None


class ConfirmIn(BaseModel):
    block_number: int | None = None


class RollbackIn(BaseModel):
    n: int = Field(default=1, ge=1)


# --------------------------------------------------------------------------- #
# 响应
# --------------------------------------------------------------------------- #
class AccountOut(BaseModel):
    address: str
    balance: str
    nonce: int


class TxOut(BaseModel):
    tx_hash: str
    sender: str
    to: str | None
    nonce: int
    gas_price: str
    gas_limit: int
    value: str
    data: str
    received_at: int
    expires_at: int
    status: str
    reason: str
    reason_detail: str
    replaced_by: str | None
    block_number: int | None
    position: int | None


class PoolStatusOut(BaseModel):
    head_number: int
    head_hash: str
    proposed_block: int | None
    pending: int
    queued: int
    included: int
    expired: int
    replaced: int
    evicted: int
    mined: int


class JournalOut(BaseModel):
    id: int
    ts: int
    request_id: str
    action: str
    tx_hash: str | None
    sender: str | None
    block_number: int | None
    from_status: str
    to_status: str
    reason: str
    detail: Any
