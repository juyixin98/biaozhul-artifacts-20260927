"""Pydantic request/response schemas (validation boundary).

Wei fields are decimal strings on the wire; pydantic converts them to ``int``
immediately so the kernel never sees a float.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class SignatureIn(BaseModel):
    r: str
    s: str
    v: int

    @field_validator("r", "s")
    @classmethod
    def _decimal(cls, v: str) -> str:
        int(v, 10)
        return v

    @field_validator("v")
    @classmethod
    def _v_range(cls, v: int) -> int:
        if v not in (0, 1):
            raise ValueError("v must be 0 or 1 (ecdsa recovery candidate index)")
        return v


class TxIn(BaseModel):
    chain_id: Optional[int] = None
    nonce: int = Field(ge=0)
    max_fee_per_gas: str
    max_priority_fee_per_gas: str
    gas_limit: int = Field(ge=0)
    to: str
    value: str
    data: str = "0x"
    signature: SignatureIn

    @field_validator("max_fee_per_gas", "max_priority_fee_per_gas", "value")
    @classmethod
    def _nonneg_decimal(cls, v: str) -> str:
        if int(v, 10) < 0:
            raise ValueError("must be >= 0")
        return v


class RawTxIn(BaseModel):
    raw: str


class BlockIn(BaseModel):
    number: int
    parent_hash: str
    base_fee_per_gas: str
    gas_limit: int
    gas_used: int
    transactions: list[dict] = Field(default_factory=list)
    raw_transactions: list[str] = Field(default_factory=list)
    strict: bool = False


class FeeQuery(BaseModel):
    parent_base_fee: str
    gas_used: int
    gas_limit: int


class FeePreviewTx(BaseModel):
    base_fee: str
    max_fee_per_gas: str
    max_priority_fee_per_gas: str
    gas_limit: int = 21000


class StepOut(BaseModel):
    parent_base_fee: str
    gas_limit: int
    gas_used: int
    target_gas: int
    direction: Literal["up", "down", "flat"]
    delta_numerator: str
    delta_after_first_floor: str
    delta_final: str
    applied_min_increment: bool
    next_base_fee: str
    denominator: int
    elasticity_multiplier: int


class ReceiptOut(BaseModel):
    tx_index: int
    tx_hash: str
    sender: str
    valid: bool
    error_code: Optional[str] = None
    gas: Optional[str] = None
    effective_priority_tip: Optional[str] = None
    effective_gas_price: Optional[str] = None
    burned: Optional[str] = None
    tip: Optional[str] = None
    total_cost: Optional[str] = None
