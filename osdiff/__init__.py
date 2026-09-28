"""osdiff: offline differential analysis of object-storage policies.

Modules:
    types     -- shared enums, sentinels and result dataclasses
    patterns  -- restricted pattern language and exhaustive trie region enumeration
    policy    -- rule parsing and validation (explicit refusal for unanalyzable input)
    kernel    -- three-valued security kernel (allow / deny / unknown)
    universe  -- bounded request-space construction
    diff      -- policy differential analysis and witness extraction
    evidence  -- request identity, canonical serialization, witness re-checking
    signing   -- local Ed25519 signatures for tamper evidence
    store     -- SQLite-backed, run-isolated persistent state
    audit     -- hash-chained, signed audit trail with correlation ids
    config    -- configuration loading
    api       -- FastAPI audit/query interface
    cli       -- command line entry point
"""

__version__ = "0.1.0"
