"""SQLite schema for the derived index and block/chain bookkeeping."""
from __future__ import annotations

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Every valid-sealed block we have ever seen (active chain, forks, orphans).
CREATE TABLE IF NOT EXISTS blocks (
    hash              TEXT PRIMARY KEY,
    height            INTEGER NOT NULL,
    parent            TEXT NOT NULL,
    weight            INTEGER NOT NULL,
    difficulty        INTEGER NOT NULL,
    producer          TEXT NOT NULL,
    timestamp         TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    first_seen_seq    INTEGER NOT NULL,
    is_active         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_blocks_height ON blocks(height);
CREATE INDEX IF NOT EXISTS idx_blocks_parent ON blocks(parent);

-- Active chain membership, exactly one row per occupied height.
CREATE TABLE IF NOT EXISTS active_chain (
    height      INTEGER PRIMARY KEY,
    block_hash  TEXT NOT NULL REFERENCES blocks(hash)
);

-- Orphans (sealed and statelessly valid, but parent not yet known).
CREATE TABLE IF NOT EXISTS pending_blocks (
    hash           TEXT PRIMARY KEY,
    parent         TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    arrived_seq    INTEGER NOT NULL
);

-- Durable two-phase switch plan, present only while a reorg is in progress.
CREATE TABLE IF NOT EXISTS switch_plan (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    new_tip           TEXT NOT NULL,
    detach_hashes_json TEXT NOT NULL,
    attach_hashes_json TEXT NOT NULL,
    phase             TEXT NOT NULL,
    request_id        TEXT NOT NULL
);

-- The revocable derived index: ledger events emitted by active-chain blocks.
CREATE TABLE IF NOT EXISTS derived_events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    block_hash     TEXT NOT NULL REFERENCES blocks(hash),
    height         INTEGER NOT NULL,
    txid           TEXT NOT NULL,
    position_in_block INTEGER NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('debit','credit','fee','nonce')),
    address        TEXT NOT NULL,
    amount_delta   INTEGER NOT NULL,
    nonce_delta    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(block_hash, position_in_block)
);
CREATE INDEX IF NOT EXISTS idx_events_address ON derived_events(address);
CREATE INDEX IF NOT EXISTS idx_events_height ON derived_events(height);
CREATE INDEX IF NOT EXISTS idx_events_txid ON derived_events(txid);

-- Materialized account view of the active chain.
CREATE TABLE IF NOT EXISTS account_state (
    address   TEXT PRIMARY KEY,
    balance   INTEGER NOT NULL,
    nonce     INTEGER NOT NULL
);

-- Transaction contributions known anywhere (all valid blocks), keyed by
-- (txid, block_hash): one row per occurrence.
CREATE TABLE IF NOT EXISTS tx_locations (
    txid         TEXT NOT NULL,
    block_hash   TEXT NOT NULL REFERENCES blocks(hash),
    height       INTEGER NOT NULL,
    on_active    INTEGER NOT NULL,
    PRIMARY KEY (txid, block_hash)
);
CREATE INDEX IF NOT EXISTS idx_txlocs_block ON tx_locations(block_hash);

-- Structured diagnostics (see reorgindex.diag).
CREATE TABLE IF NOT EXISTS diagnostics (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id   TEXT NOT NULL,
    outcome      TEXT NOT NULL,
    reason       TEXT,
    block_hash   TEXT,
    height       INTEGER,
    parent       TEXT,
    active_tip   TEXT,
    active_height INTEGER,
    weight       INTEGER,
    detail       TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diag_request ON diagnostics(request_id);
CREATE INDEX IF NOT EXISTS idx_diag_created ON diagnostics(created_at);

-- Monotonic arrival counter.
CREATE TABLE IF NOT EXISTS counters (
    name    TEXT PRIMARY KEY,
    value   INTEGER NOT NULL
);
"""
