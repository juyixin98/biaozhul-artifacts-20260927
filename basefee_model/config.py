"""Fixed protocol parameters and configuration.

These constants are the *semantic* knobs of the model. They are fixed here on
purpose: the requirements call for explicit, fixed parameters so that hand
computed vectors are reproducible. They are not learned, not forecast and never
read from a live chain.

The recurrence itself (see :mod:`basefee_model.core.fees`) uses exactly the
EIP-1559 integer arithmetic as implemented by go-ethereum v1.10.26
(``consensus/misc/eip1559.go``::``CalcBaseFee``):

* gasTarget          = gasLimit // ELASTICITY_MULTIPLIER
* used == target     -> next = parentBaseFee
* used >  target     -> next = parentBaseFee + max(1,
                          (used-target)*parentBaseFee // gasTarget // CHANGE_DENOM)
* used <  target     -> next = max(0,
                          parentBaseFee -
                            (target-used)*parentBaseFee // gasTarget // CHANGE_DENOM)

``//`` is mathematical floor division on non-negative integers; every operand
is non-negative, so floor and truncation coincide here.
"""

from __future__ import annotations

from dataclasses import dataclass
import os

from . import __version__

# --- EIP-1559 fee-market constants (fixed) --------------------------------
ELASTICITY_MULTIPLIER = 2          # gasTarget = gasLimit / 2
BASE_FEE_CHANGE_DENOMINATOR = 8    # 1/8 max change per block
MIN_BASE_FEE = 0                   # base fee can never go below zero
INITIAL_BASE_FEE = 1_000_000_000   # genesis base fee = 1 gwei (wei)

# --- Block / transaction limits (fixed, synthetic network) -----------------
# 30,000,000 is deliberately a round synthetic value; target is half of it.
DEFAULT_GAS_LIMIT = 30_000_000
MIN_GAS_LIMIT = 1
# EVM integer bounds used for overflow / fee-cap validation.
MAX_U256 = (1 << 256) - 1
MAX_U64 = (1 << 64) - 1
# Up-front cost of a transaction must not overflow the EVM's 256-bit word.
# A cap above this value is therefore inherently invalid.

# Intrinsic gas (EIP-2028 calibrated). Calldata is the only payload in our
# synthetic transactions; RLP envelope + signature are not charged separately
# (boundary documented in README#semantics).
INTRINSIC_GAS_TX_BASE = 21_000
INTRINSIC_GAS_ZERO_BYTE = 4
INTRINSIC_GAS_NONZERO_BYTE = 16

# Chain id for the EIP-155 / EIP-1559 signing domains (purely local/synthetic).
DEFAULT_CHAIN_ID = 1559

# Transaction envelope types we encode and validate.
TX_TYPE_LEGACY = 0
TX_TYPE_EIP1559 = 2


@dataclass(frozen=True)
class Settings:
    """Runtime (deployment) settings, kept separate from protocol constants.

    Protocol semantics live in module constants above; these only tell the
    app where files are and how to identify the run.
    """

    db_path: str = "basefee_model.db"
    chain_id: int = DEFAULT_CHAIN_ID
    genesis_base_fee: int = INITIAL_BASE_FEE
    version: str = __version__

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=os.environ.get("BASEFEE_DB_PATH", "basefee_model.db"),
            chain_id=int(os.environ.get("BASEFEE_CHAIN_ID", DEFAULT_CHAIN_ID)),
            genesis_base_fee=int(
                os.environ.get("BASEFEE_GENESIS_BASE_FEE", INITIAL_BASE_FEE)
            ),
        )
