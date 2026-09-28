"""Pydantic request/response schemas for the HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, Field


# -- /basefee/next ----------------------------------------------------------
class NextBaseFeeRequest(BaseModel):
    parent_base_fee: int = Field(..., ge=0, description="Parent block base fee (wei)")
    parent_gas_used: int = Field(..., ge=0, description="Parent block gas used")
    parent_gas_limit: int = Field(..., ge=1, description="Parent block gas limit")


# -- /transactions/validate-fee --------------------------------------------
class ValidateFeeRequest(BaseModel):
    tx_type: int = Field(..., description="0=legacy, 2=EIP-1559")
    base_fee: int = Field(..., ge=0)
    gas_limit: int = Field(..., ge=0)
    value: int = Field(0, ge=0)
    data_hex: str = Field("0x", description="Hex calldata for intrinsic gas")
    max_fee_per_gas: int | None = Field(None, ge=0)
    max_priority_fee_per_gas: int | None = Field(None, ge=0)
    gas_price: int | None = Field(None, ge=0)


# -- /replay ----------------------------------------------------------------
class ReplayBlock(BaseModel):
    number: int = Field(..., ge=1)
    raw_transactions: list[str] = Field(default_factory=list)
    tx_gas_used: list[int] = Field(default_factory=list)


class ReplayRequest(BaseModel):
    genesis_base_fee: int = Field(1_000_000_000, ge=0)
    gas_limit: int = Field(30_000_000, ge=1)
    chain_id: int = Field(1559, ge=1, description="Enforced EIP-155 chain id")
    genesis_gas_used: int | None = Field(None)
    alloc: dict[str, int] = Field(
        default_factory=dict,
        description="0x address -> genesis balance (wei)",
    )
    blocks: list[ReplayBlock]


class StepView(BaseModel):
    parent_base_fee: int
    parent_gas_used: int
    parent_gas_limit: int
    gas_target: int
    direction: str
    delta: int
    next_base_fee: int


class ErrorView(BaseModel):
    code: str
    message: str
    details: dict = Field(default_factory=dict)
    component: str
    request_id: str
