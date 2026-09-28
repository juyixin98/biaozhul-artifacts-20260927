"""Frozen protocol constants for the test-asset UTXO ledger.

These constants are the single source of truth shared by encoding, kernel,
storage and API. The independent test oracle (``tests/oracle.py``) deliberately
does **not** import this module: it hard-codes the same values from the written
specification so that a mistaken change here cannot silently make both sides
agree.
"""

# --- domain-separated tags (fixed encoding, never reuse a tag) --------------
TX_TAG = b"UTXO-TXN/1\x00"
BLOCK_TAG = b"UTXO-BLOCK/1\x00"
SIGHASH_TAG = b"UTXO-SIGHASH/1\x00"

# --- fixed sizes ------------------------------------------------------------
TXID_LEN = 32
HASH_LEN = 32
PUBLIC_KEY_LEN = 32          # Ed25519 compressed points
SIGNATURE_LEN = 64           # Ed25519 signatures
MAX_PUBKEY_BYTES = 64        # structural reject above this (generic bound)
MAX_SIGNATURE_BYTES = 512    # structural reject above this (generic bound)
OUTPOINT_LEN = TXID_LEN + 4  # txid + u32 vout

# --- value rules (integer satoshi-like units) -------------------------------
MAX_MONEY = 2**64 - 1        # every amount and every validated sum fits u64

# --- block/transaction resource limits --------------------------------------
MAX_BLOCK_RAW = 1_000_000
MAX_BLOCK_TXS = 1_000
MAX_BLOCK_SIGOPS = 10_000
MAX_TX_INPUTS = 256
MAX_TX_OUTPUTS = 256

# --- issuance ----------------------------------------------------------------
BLOCK_SUBSIDY = 1_000_000     # test asset only; no halving schedule

# --- chain -------------------------------------------------------------------
GENESIS_PREV_HASH = b"\x00" * HASH_LEN
GENESIS_HEIGHT = 0

# Alias kept for ergonomic imports (genesis / empty-chain sentinel).
ZERO_HASH = GENESIS_PREV_HASH

SUPPORTED_TX_VERSION = 1
SUPPORTED_BLOCK_VERSION = 1
