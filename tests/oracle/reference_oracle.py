"""Independent reference oracle for EIP-1559 fee math.

CRITICAL: this module intentionally does **not** import anything from
``basefee_model.core``. It is a second, independently written implementation of
the same specification (EIP-1559 / go-ethereum ``CalcBaseFee``), used to
cross-check the system under test. If both happened to share a bug, this oracle
would not catch it -- therefore the *anchor* vectors in
``tests/vectors/hand_vectors.json`` are additionally written down from literal
hand arithmetic, and tests assert all three sources agree.

The effective-price rule is likewise re-derived from the EIP prose rather than
calling the production code:

    type-2:  effective = min(max_fee, base_fee + priority)
             tip       = effective - base_fee
    legacy:  effective = gas_price
             tip       = gas_price - base_fee (>= 0)
"""

from __future__ import annotations

EM = 2      # elasticity multiplier
DEN = 8     # base-fee change denominator
FLOOR = 0


def oracle_gas_target(gas_limit: int) -> int:
    return gas_limit // EM


def oracle_next_base_fee(parent_base_fee: int, parent_gas_used: int,
                         parent_gas_limit: int) -> int:
    """Standalone reimplementation; floor division on non-negative ints."""
    if parent_gas_used > parent_gas_limit:
        raise ValueError("used over limit")
    target = parent_gas_limit // EM
    if parent_gas_used == target:
        return parent_base_fee
    if parent_gas_used > target:
        quotient = ((parent_gas_used - target) * parent_base_fee
                    // target // DEN)
        # EIP: if baseFee increases, it must increase by at least 1.
        if quotient < 1:
            quotient = 1
        return parent_base_fee + quotient
    decrease = ((target - parent_gas_used) * parent_base_fee
                // target // DEN)
    candidate = parent_base_fee - decrease
    return candidate if candidate > FLOOR else FLOOR


def oracle_effective_price(tx_type: int, base_fee: int, *,
                           max_fee: int = 0, priority: int = 0,
                           gas_price: int = 0) -> tuple[int, int]:
    """Return (effective_gas_price, priority_tip)."""
    if tx_type == 0:
        return gas_price, max(0, gas_price - base_fee)
    eff = min(max_fee, base_fee + priority)
    return eff, eff - base_fee


def oracle_chain(steps: list[tuple[int, int]], base_fee: int,
                 gas_limit: int) -> list[int]:
    """Given per-block gas-used, return base fee *at* each block (len steps).

    The first block uses ``base_fee`` directly as its parent base fee; i.e.
    ``steps[0]`` is the parent (genesis) gas used paired with ``base_fee``.
    """
    out = []
    for used in steps:
        base_fee = oracle_next_base_fee(base_fee, used, gas_limit)
        out.append(base_fee)
    return out
