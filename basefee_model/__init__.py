"""EIP-1559-style base-fee recurrence model (synthetic, offline).

A multi-module backend that implements, with real mechanics (no hard-coded
demos), the following pipeline::

    encoding & signature recovery (RLP + secp256k1 + Keccak-256)
        -> chain-state kernel (EIP-1559 fee math, validation, charge/burn)
        -> indexed storage (SQLite)
        -> offline replay (deterministic, idempotent)

The model has NO live chain connection and NO economic forecasting: the next
base fee is a pure function of the parent block's parameters.
"""

__version__ = "1.0.0"
