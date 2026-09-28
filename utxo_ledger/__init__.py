"""Test-asset UTXO ledger.

Module boundaries (see README.md "边界与契约"):

* :mod:`utxo_ledger.protocol`  -- wire tags, sizes, limits, subsidy (frozen contract).
* :mod:`utxo_ledger.errors`    -- error codes, categories and ``LedgerError`` contract.
* :mod:`utxo_ledger.crypto`    -- Ed25519 signature verification (mature library only).
* :mod:`utxo_ledger.encoding`  -- canonical fixed binary encoding + sighash + structural decode.
* :mod:`utxo_ledger.kernel`    -- pure chain-state validation (no I/O) plus ``LedgerNode``.
* :mod:`utxo_ledger.storage`   -- SQLite indexed UTXO set / block / tx / spend history.
* :mod:`utxo_ledger.runlog`    -- structured JSONL run logs with stable run ids.
* :mod:`utxo_ledger.replay`    -- offline block replay / verification (library + CLI).
* :mod:`utxo_ledger.api`       -- FastAPI HTTP boundary.
"""

from .errors import ErrorCategory, ErrorCode, LedgerError
from .protocol import BLOCK_SUBSIDY, ZERO_HASH

__all__ = [
    "ErrorCategory",
    "ErrorCode",
    "LedgerError",
    "BLOCK_SUBSIDY",
    "ZERO_HASH",
]
