-- Chandy-Lamport teaching service: per-node schema.
-- Three processes use three schemas (n1, n2, n3) in one database so a run is
-- easy to inspect, but nothing in the code assumes shared state between nodes.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
    account  TEXT PRIMARY KEY,
    balance  BIGINT NOT NULL CHECK (balance >= 0)
);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id         BIGSERIAL PRIMARY KEY,
    to_peer    TEXT NOT NULL,
    seq        BIGINT NOT NULL,
    envelope   JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (to_peer, seq)
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox (to_peer, seq);

CREATE TABLE IF NOT EXISTS incoming_seq (
    peer TEXT PRIMARY KEY,
    seq  BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS seen_messages (
    msg_id     TEXT PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    id         BIGSERIAL PRIMARY KEY,
    run_id     TEXT NOT NULL,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind       TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    lamport    BIGINT NOT NULL DEFAULT 0,
    detail     JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS events_session ON events (session_id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    initiator    TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('recording','complete','aborted')),
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    aborted_at   TIMESTAMPTZ,
    abort_reason TEXT NOT NULL DEFAULT '',
    local_lamport     BIGINT,
    local_recorded_at TIMESTAMPTZ,
    local_balances    JSONB,
    local_total       BIGINT
);

CREATE TABLE IF NOT EXISTS channels (
    session_id    TEXT NOT NULL REFERENCES sessions(session_id),
    from_peer     TEXT NOT NULL,
    recorded      JSONB NOT NULL DEFAULT '[]'::jsonb,
    total_inflight BIGINT NOT NULL DEFAULT 0,
    marker_seen_at  TIMESTAMPTZ,
    marker_lamport  BIGINT,
    PRIMARY KEY (session_id, from_peer)
);
