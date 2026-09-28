"""Transaction admission / validity checks.

Pure, side-effect-free checks (intrinsic gas, fee-cap relationships, overflow,
nonce and balance) live here and return a categorized result. The stateful
application of an admitted transaction (charging/burning) lives in
:mod:`basefee_model.core.state`.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import (INTRINSIC_GAS_NONZERO_BYTE, INTRINSIC_GAS_TX_BASE,
                      INTRINSIC_GAS_ZERO_BYTE, MAX_U256)
from ..errors import FailureCode, TransactionError
from ..encoding.transaction import Transaction


@dataclass(frozen=True)
class FeeQuote:
    """The actual charges implied by a transaction at a given base fee."""
    effective_gas_price: int
    priority_fee_per_gas: int
    base_fee_per_gas: int
    gas_limit: int
    max_upfront: int          # fee_cap * gas_limit + value (admission bound)
    actual_fee_max: int       # effective_price * gas_limit (execution bound)


def intrinsic_gas(tx: Transaction) -> int:
    """EIP-2028 intrinsic gas: 21000 + 4/zero byte + 16/non-zero byte."""
    gas = INTRINSIC_GAS_TX_BASE
    for byte in tx.data:
        gas += INTRINSIC_GAS_ZERO_BYTE if byte == 0 else INTRINSIC_GAS_NONZERO_BYTE
    return gas


def fee_quote(tx: Transaction, base_fee: int) -> FeeQuote:
    eff = tx.effective_gas_price(base_fee)
    prio = tx.priority_fee_per_gas(base_fee)
    base_portion = eff - prio  # the part that gets burned
    return FeeQuote(
        effective_gas_price=eff,
        priority_fee_per_gas=prio,
        base_fee_per_gas=base_portion,
        gas_limit=tx.gas_limit,
        max_upfront=tx_max_upfront(tx),
        actual_fee_max=eff * tx.gas_limit,
    )


def tx_max_upfront(tx: Transaction) -> int:
    fee_cap = tx.gas_price if tx.type == 0 else tx.max_fee_per_gas
    return fee_cap * tx.gas_limit + tx.value


def validate_fee_caps(tx: Transaction, base_fee: int) -> None:
    """Fee-cap and overflow validation (applies to both envelope types)."""
    if tx.type == 0:
        if tx.gas_price > MAX_U256:
            raise TransactionError(
                "gas_price overflows uint256",
                code=FailureCode.FEE_CAP_OVERFLOWS_U256,
                details={"gas_price": tx.gas_price},
            )
        # Legacy: fee cap is gas_price for both cap and priority.
        if tx.gas_price < base_fee:
            raise TransactionError(
                "gas_price below current base fee",
                code=FailureCode.FEE_CAP_BELOW_BASE_FEE,
                details={"gas_price": tx.gas_price, "base_fee": base_fee},
            )
        return

    if tx.max_fee_per_gas > MAX_U256:
        raise TransactionError(
            "max_fee_per_gas overflows uint256",
            code=FailureCode.FEE_CAP_OVERFLOWS_U256,
            details={"max_fee_per_gas": tx.max_fee_per_gas},
        )
    if tx.max_priority_fee_per_gas > MAX_U256:
        raise TransactionError(
            "max_priority_fee_per_gas overflows uint256",
            code=FailureCode.PRIORITY_CAP_OVERFLOWS_U256,
            details={"max_priority_fee_per_gas": tx.max_priority_fee_per_gas},
        )
    if tx.max_fee_per_gas < tx.max_priority_fee_per_gas:
        raise TransactionError(
            "max_fee_per_gas must be >= max_priority_fee_per_gas",
            code=FailureCode.FEE_CAP_LESS_THAN_PRIORITY,
            details={"max_fee": tx.max_fee_per_gas,
                     "max_priority_fee": tx.max_priority_fee_per_gas},
        )
    if tx.max_fee_per_gas < base_fee:
        raise TransactionError(
            "max_fee_per_gas below current base fee; transaction cannot pay",
            code=FailureCode.FEE_CAP_BELOW_BASE_FEE,
            details={"max_fee": tx.max_fee_per_gas, "base_fee": base_fee},
        )
    # Overflow check on the up-front reservation (cap*gas + value).
    upfront = tx_max_upfront(tx)
    if upfront > MAX_U256:
        raise TransactionError(
            "up-front cost (fee_cap*gas_limit + value) overflows uint256",
            code=FailureCode.FEE_CAP_OVERFLOWS_U256,
            details={"upfront": upfront},
        )


def validate_intrinsic_gas(tx: Transaction) -> int:
    ig = intrinsic_gas(tx)
    if tx.gas_limit < ig:
        raise TransactionError(
            "gas_limit below intrinsic gas",
            code=FailureCode.GAS_LIMIT_EXCEEDED_INTRINSIC,
            details={"gas_limit": tx.gas_limit, "intrinsic_gas": ig},
        )
    return ig
