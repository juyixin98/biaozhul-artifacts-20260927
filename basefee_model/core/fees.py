"""EIP-1559 base-fee recurrence: the heart of the model.

The next base fee is a **pure function of the parent block's parameters**
(parent base fee, parent gas used, parent gas limit). Nothing else enters it.

Integer semantics follow go-ethereum v1.10.26 ``CalcBaseFee`` exactly
(verified against the upstream source):

    target = gasLimit // ELASTICITY_MULTIPLIER

    gasUsed == target          -> nextBaseFee = parentBaseFee
    gasUsed >  target (up)     -> delta = (gasUsed - target) * parentBaseFee
                                            // target // CHANGE_DENOMINATOR
                                  nextBaseFee = parentBaseFee + max(delta, 1)
    gasUsed <  target (down)   -> delta = (target - gasUsed) * parentBaseFee
                                            // target // CHANGE_DENOMINATOR
                                  nextBaseFee = max(parentBaseFee - delta, 0)

All operands are non-negative so Python's ``//`` (floor) and the JVM/EVM-style
truncation give the same quotient.
"""

from __future__ import annotations

from ..config import (BASE_FEE_CHANGE_DENOMINATOR, ELASTICITY_MULTIPLIER,
                      MIN_BASE_FEE)
from ..errors import BlockError, FailureCode


def gas_target(gas_limit: int) -> int:
    if gas_limit < 1:
        raise BlockError("gas limit must be >= 1",
                         code=FailureCode.INVALID_FIELDS)
    return gas_limit // ELASTICITY_MULTIPLIER


def next_base_fee(parent_base_fee: int, parent_gas_used: int,
                  parent_gas_limit: int) -> int:
    """Pure EIP-1559 recurrence. See module docstring for exact semantics."""
    if parent_base_fee < 0:
        raise BlockError("parent base fee must be >= 0",
                         code=FailureCode.BAD_BASE_FEE)
    if parent_gas_used < 0:
        raise BlockError("parent gas used must be >= 0",
                         code=FailureCode.BLOCK_GAS_NEGATIVE)
    if parent_gas_used > parent_gas_limit:
        raise BlockError(
            "parent gas used exceeds parent gas limit",
            code=FailureCode.BLOCK_GAS_OVER_LIMIT,
            details={"gas_used": parent_gas_used,
                     "gas_limit": parent_gas_limit},
        )

    target = gas_target(parent_gas_limit)

    if parent_gas_used == target:
        return parent_base_fee

    if parent_gas_used > target:
        gas_delta = parent_gas_used - target
        delta = (gas_delta * parent_base_fee
                 // target // BASE_FEE_CHANGE_DENOMINATOR)
        # Minimum upward increment is exactly 1 wei (geth: math.BigMax(num, 1)).
        delta = max(delta, 1)
        return parent_base_fee + delta

    gas_delta = target - parent_gas_used
    delta = (gas_delta * parent_base_fee // target
             // BASE_FEE_CHANGE_DENOMINATOR)
    return max(parent_base_fee - delta, MIN_BASE_FEE)


def base_fee_step_report(parent_base_fee: int, parent_gas_used: int,
                         parent_gas_limit: int) -> dict:
    """Explain one recurrence step with the intermediate values, for logs."""
    target = gas_target(parent_gas_limit)
    if parent_gas_used == target:
        direction, delta = "flat", 0
    elif parent_gas_used > target:
        direction = "up"
        raw = ((parent_gas_used - target) * parent_base_fee
               // target // BASE_FEE_CHANGE_DENOMINATOR)
        delta = max(raw, 1)
    else:
        direction = "down"
        delta = ((target - parent_gas_used) * parent_base_fee
                 // target // BASE_FEE_CHANGE_DENOMINATOR)
    nxt = next_base_fee(parent_base_fee, parent_gas_used, parent_gas_limit)
    return {
        "parent_base_fee": parent_base_fee,
        "parent_gas_used": parent_gas_used,
        "parent_gas_limit": parent_gas_limit,
        "gas_target": target,
        "direction": direction,
        "delta": delta,
        "next_base_fee": nxt,
    }
