"""Core EIP-1559-style base-fee recurrence (pure arithmetic, no I/O).

Recurrence (the next block's fee depends *only* on the parent block):

    target = gas_limit // ELASTICITY_MULTIPLIER          # = gas_limit / 2

    if gas_used == target:
        next_base_fee = base_fee
    elif gas_used > target:
        delta = base_fee * (gas_used - target) // target
        delta = delta // BASE_FEE_MAX_CHANGE_DENOMINATOR
        next_base_fee = base_fee + max(delta, 1)        # minimum +1 wei
    else:
        delta = base_fee * (target - gas_used) // target
        delta = delta // BASE_FEE_MAX_CHANGE_DENOMINATOR
        next_base_fee = base_fee - delta
        next_base_fee = max(next_base_fee, MIN_BASE_FEE)

Integer semantics
-----------------
* Every division is a *floor* division (``//``) on non-negative integers and is
  performed in two sequential floors, matching the deployed implementation.
* The upward path enforces a **minimum increment of 1 wei** whenever the block
  is strictly above target (otherwise tiny base fees would be permanently stuck).
* The downward path has no minimum-decrement rule; a sub-1-wei computed
  reduction rounds to zero (fee is "sticky"), again matching deployment.

Effective fee paid by a transaction (also pure):

    priority_tip     = min(max_priority_fee, max_fee - base_fee)
    effective_price  = base_fee + priority_tip
    burned           = base_fee * gas
    tip              = priority_tip * gas
"""

from __future__ import annotations

from dataclasses import dataclass

from ..params import PARAMS, ProtocolParams


@dataclass(frozen=True)
class FeeStep:
    """Explainable, step-by-step result of one recurrence application."""

    parent_base_fee: int
    gas_limit: int
    gas_used: int
    target_gas: int
    direction: str  # "up" | "down" | "flat"
    delta_numerator: int  # base_fee * |gas_used - target|
    delta_after_first_floor: int
    delta_final: int
    applied_min_increment: bool
    next_base_fee: int


def target_gas(gas_limit: int, params: ProtocolParams = PARAMS) -> int:
    return params.target_gas_for(gas_limit)


def next_base_fee(parent_base_fee: int, gas_used: int, gas_limit: int,
                  params: ProtocolParams = PARAMS) -> int:
    """Compute the next block's base fee from the parent block's parameters."""
    return _step(parent_base_fee, gas_used, gas_limit, params).next_base_fee


def compute_next_base_fee_step(parent_base_fee: int, gas_used: int, gas_limit: int,
                               params: ProtocolParams = PARAMS) -> FeeStep:
    """Same recurrence, but returning every intermediate value for explainability."""
    return _step(parent_base_fee, gas_used, gas_limit, params)


def _step(parent_base_fee: int, gas_used: int, gas_limit: int,
          params: ProtocolParams) -> FeeStep:
    if parent_base_fee < 0:
        raise ValueError("parent_base_fee must be >= 0")
    if gas_limit < 0:
        raise ValueError("gas_limit must be >= 0")
    if gas_used < 0:
        raise ValueError("gas_used must be >= 0")
    if gas_used > gas_limit:
        # Gas cap violation is surfaced as a distinct category by callers; the
        # pure function refuses the input rather than returning a number.
        raise _GasExceeded(parent_base_fee, gas_used, gas_limit)
    target = params.target_gas_for(gas_limit)
    denom = params.base_fee_max_change_denominator
    elasticity = params.elasticity_multiplier
    # Defensive: target formula assumes gas_limit divisible by elasticity.
    if gas_limit % elasticity != 0:
        raise ValueError("gas_limit not divisible by elasticity_multiplier")

    if gas_used == target:
        return FeeStep(parent_base_fee, gas_limit, gas_used, target, "flat",
                       0, 0, 0, False, parent_base_fee)

    if gas_used > target:
        gap = gas_used - target
        numerator = parent_base_fee * gap
        first_floor = numerator // target
        raw_delta = first_floor // denom
        # Minimum +1 wei only when a positive parent fee is at stake: the fee
        # cannot be manufactured out of nothing at parent_base_fee = 0.
        if raw_delta == 0 and parent_base_fee > 0:
            delta = 1
        else:
            delta = raw_delta
        applied_min = delta != raw_delta
        nxt = parent_base_fee + delta
        return FeeStep(parent_base_fee, gas_limit, gas_used, target, "up",
                       numerator, first_floor, delta, applied_min, nxt)

    gap = target - gas_used
    numerator = parent_base_fee * gap
    first_floor = numerator // target
    delta = first_floor // denom
    nxt = max(parent_base_fee - delta, params.min_base_fee)
    return FeeStep(parent_base_fee, gas_limit, gas_used, target, "down",
                   numerator, first_floor, delta, False, nxt)


class _GasExceeded(ValueError):
    """Internal: gas_used > gas_limit. Mapped to E040 by the kernel/API."""

    def __init__(self, base_fee: int, gas_used: int, gas_limit: int):
        super().__init__(f"gas_used {gas_used} exceeds gas_limit {gas_limit}")
        self.parent_base_fee = base_fee
        self.gas_used = gas_used
        self.gas_limit = gas_limit


def gas_exceeds_limit(gas_used: int, gas_limit: int) -> bool:
    return gas_used > gas_limit


def effective_priority_tip(*, base_fee: int, max_fee_per_gas: int,
                           max_priority_fee_per_gas: int) -> int:
    """min(tip_cap, max_fee - base_fee); never negative (caller rejects below-base)."""
    slack = max_fee_per_gas - base_fee
    if max_priority_fee_per_gas < slack:
        return max_priority_fee_per_gas
    return slack


def effective_gas_price(*, base_fee: int, max_fee_per_gas: int,
                        max_priority_fee_per_gas: int) -> int:
    return base_fee + effective_priority_tip(
        base_fee=base_fee,
        max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
    )
