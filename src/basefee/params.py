"""Fixed protocol parameters.

These are the *immutable* fixed parameters of the synthetic fee model. They are
loaded once from ``config/protocol.json`` (explicit, reviewable configuration)
and exposed as a frozen dataclass. Changing any of them is a protocol change:
bump ``protocol_version`` rather than mutating values at runtime.

Boundary semantics
-------------------
* Integer division direction for the fee recurrence is *floor* (Python ``//``
  on non-negative values), applied in two sequential floor divisions exactly as
  the deployed EIP-1559 implementation does. A separate minimum-increment rule
  guarantees that an above-target block moves the fee by at least 1 wei.
* The model contains no network connection and no economic forecasting; see
  ``docs/SEMANTICS.md`` for the full boundary description.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Max uint256. Used as the arithmetic ceiling for fee fields: any product larger
# than this is an overflow (error code E032).
UINT256_MAX = 2**256 - 1

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "protocol.json"


@dataclass(frozen=True)
class ProtocolParams:
    """Immutable snapshot of the fixed protocol parameters."""

    chain_id: int
    protocol_version: str
    genesis_number: int
    genesis_base_fee: int
    genesis_gas_used: int
    genesis_gas_limit: int
    elasticity_multiplier: int
    base_fee_max_change_denominator: int
    intrinsic_tx_gas: int
    min_base_fee: int
    max_fee_wei_limit: int
    address_hex_length: int

    def target_gas_for(self, gas_limit: int) -> int:
        """Target gas for a block carrying ``gas_limit`` (must be evenly divisible)."""
        if gas_limit <= 0 or gas_limit % self.elasticity_multiplier != 0:
            raise ValueError(
                f"gas_limit must be positive and divisible by elasticity_multiplier "
                f"({self.elasticity_multiplier}), got {gas_limit}"
            )
        return gas_limit // self.elasticity_multiplier


def _load(path: Path = _CONFIG_PATH) -> ProtocolParams:
    data = json.loads(path.read_text(encoding="utf-8"))
    p = ProtocolParams(
        chain_id=int(data["chain_id"]),
        protocol_version=str(data["protocol_version"]),
        genesis_number=int(data["genesis_number"]),
        genesis_base_fee=int(data["genesis_base_fee"]),
        genesis_gas_used=int(data["genesis_gas_used"]),
        genesis_gas_limit=int(data["genesis_gas_limit"]),
        elasticity_multiplier=int(data["elasticity_multiplier"]),
        base_fee_max_change_denominator=int(data["base_fee_max_change_denominator"]),
        intrinsic_tx_gas=int(data["intrinsic_tx_gas"]),
        min_base_fee=int(data["min_base_fee"]),
        max_fee_wei_limit=int(data["max_fee_wei_limit"]),
        address_hex_length=int(data["address_hex_length"]),
    )
    _validate(p)
    return p


def _validate(p: ProtocolParams) -> None:
    if p.elasticity_multiplier < 1:
        raise ValueError("elasticity_multiplier must be >= 1")
    if p.base_fee_max_change_denominator < 1:
        raise ValueError("base_fee_max_change_denominator must be >= 1")
    if p.intrinsic_tx_gas < 1:
        raise ValueError("intrinsic_tx_gas must be >= 1")
    if p.genesis_gas_used > p.genesis_gas_limit:
        raise ValueError("genesis gas_used must not exceed gas_limit")
    if p.genesis_gas_limit % p.elasticity_multiplier != 0:
        raise ValueError("genesis_gas_limit must be divisible by elasticity_multiplier")
    if p.max_fee_wei_limit != UINT256_MAX:
        raise ValueError("max_fee_wei_limit must equal uint256 max for this synthetic fork")
    if p.min_base_fee < 0:
        raise ValueError("min_base_fee must be >= 0")


PARAMS: ProtocolParams = _load()
