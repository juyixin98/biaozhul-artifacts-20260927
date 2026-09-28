-- DHCPv4 subset durable state. Timestamps are unix-nanoseconds (INTEGER)
-- in UTC; 0 means "not set".

PRAGMA application_id = 0x44484350; -- 'DHCP'

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', '1');

CREATE TABLE IF NOT EXISTS clients (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,         -- dhcp4.ClientIdentity.Key()
    htype       INTEGER NOT NULL,
    chaddr      TEXT NOT NULL,                -- colon hex
    option_id   BLOB,                         -- raw option 61 when used
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS offers (
    id          INTEGER PRIMARY KEY,
    ip          BLOB NOT NULL,                -- 4-byte IPv4
    client_id   INTEGER NOT NULL REFERENCES clients(id),
    xid         BLOB NOT NULL,                -- 4 bytes
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    superseded  INTEGER NOT NULL DEFAULT 0    -- 1 once replaced/committed
);

-- At most one LIVE offer per IP: this is what makes OFFER a reservation
-- without making it a lease. Expired rows are flipped to superseded=1 by
-- the (transactional) sweep before any allocation, so only the flag is
-- needed in the predicate.
CREATE UNIQUE INDEX IF NOT EXISTS ux_offers_live_ip
    ON offers(ip) WHERE superseded = 0;

-- One live offer per client (a new DISCOVER supersedes the previous one).
CREATE UNIQUE INDEX IF NOT EXISTS ux_offers_live_client
    ON offers(client_id) WHERE superseded = 0;

CREATE TABLE IF NOT EXISTS leases (
    id          INTEGER PRIMARY KEY,
    ip          BLOB NOT NULL,
    client_id   INTEGER NOT NULL REFERENCES clients(id),
    state       TEXT NOT NULL CHECK (state IN ('leased','released','expired')),
    xid         BLOB NOT NULL,                -- xid that created the lease
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    ended_at    INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    version     INTEGER NOT NULL DEFAULT 1
);

-- The core uniqueness guarantee: at most one live ('leased') row per IP.
CREATE UNIQUE INDEX IF NOT EXISTS ux_leases_live_ip
    ON leases(ip) WHERE state = 'leased';

-- A client has at most one live lease; selecting a new address ends the
-- old row as 'released' first.
CREATE UNIQUE INDEX IF NOT EXISTS ux_leases_live_client
    ON leases(client_id) WHERE state = 'leased';

CREATE INDEX IF NOT EXISTS ix_leases_client ON leases(client_id, state);
CREATE INDEX IF NOT EXISTS ix_leases_expiry ON leases(state, expires_at);

-- Reply/journal log: drives duplicate detection, verbatim replay, and
-- the diagnostics API.
CREATE TABLE IF NOT EXISTS replies (
    id           INTEGER PRIMARY KEY,
    created_at   INTEGER NOT NULL,
    client_key   TEXT NOT NULL,              -- may be "" for unknown senders
    xid          BLOB NOT NULL,
    recv_type    TEXT NOT NULL,              -- DISCOVER/REQUEST/RELEASE
    action       TEXT NOT NULL,              -- offer/ack/nak/drop/...
    reply_type   TEXT NOT NULL DEFAULT '',   -- OFFER/ACK/NAK or ''
    offered_ip   BLOB,                        -- 4 bytes or NULL
    lease_expires INTEGER NOT NULL DEFAULT 0,
    -- ref_id pinpoints the exact offer/lease ROW this reply granted, so
    -- duplicate liveness is matched to that generation rather than to
    -- "any live row for the same IP". NULL for NAK/drop/no-reference rows.
    ref_kind     TEXT NOT NULL DEFAULT '',   -- 'offer' | 'lease' | ''
    ref_id       INTEGER NOT NULL DEFAULT 0,
    reason       TEXT NOT NULL DEFAULT '',
    reply_bytes  BLOB                         -- verbatim wire reply, NULL if none
);

-- Duplicate reply key: per (client, xid, received message type). Only
-- rows that still represent a live protocol response participate, so an
-- expired transaction falls through to fresh processing.
CREATE UNIQUE INDEX IF NOT EXISTS ux_replies_dedup
    ON replies(client_key, xid, recv_type)
    WHERE action IN ('offer','ack','nak');

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    ts          INTEGER NOT NULL,
    client_key  TEXT NOT NULL,
    xid         BLOB,
    kind        TEXT NOT NULL,
    detail      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
