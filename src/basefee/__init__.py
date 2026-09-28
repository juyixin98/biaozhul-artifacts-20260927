"""Synthetic EIP-1559-style base-fee recurrence model.

Modules:
* encoding  — RLP, SHA-256 identity hashing, secp256k1 sign/recover/verify
* kernel    — fee recurrence, transaction validity, block state transition
* storage   — SQLite durable index
* api       — FastAPI surface
* replay    — offline block replay driver
"""

__version__ = "1.0.0"
