-- workbroker schema revision 1
--
-- Messages retain acked/dead terminal rows so late receipts can still be
-- classified (stale vs expired vs unknown) and so the append-only event log
-- can be replayed into a full state snapshot.

CREATE TABLE IF NOT EXISTS messages (
    id                   TEXT PRIMARY KEY,
    partition_key        TEXT NOT NULL,
    body                 BYTEA NOT NULL,
    status               TEXT NOT NULL CHECK (status IN ('available','inflight','dead','acked')),
    attempts             BIGINT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts         BIGINT NOT NULL CHECK (max_attempts >= 1),
    available_at         TIMESTAMPTZ NOT NULL,
    inflight_at          TIMESTAMPTZ,
    visibility_deadline  TIMESTAMPTZ,
    receipt_id           TEXT,
    worker_id            TEXT,
    dead_reason          JSONB,
    created_at           TIMESTAMPTZ NOT NULL,
    updated_at           TIMESTAMPTZ NOT NULL
);

-- Receive's hot path: due visible messages in partition FIFO (created_at,id).
CREATE INDEX IF NOT EXISTS idx_messages_receive
    ON messages (partition_key, created_at, id)
    WHERE status = 'available';

-- Reaper's hot path: expired inflight messages.
CREATE INDEX IF NOT EXISTS idx_messages_inflight
    ON messages (visibility_deadline)
    WHERE status = 'inflight';

-- Dead-lister.
CREATE INDEX IF NOT EXISTS idx_messages_dead
    ON messages (partition_key, created_at)
    WHERE status = 'dead';

CREATE TABLE IF NOT EXISTS receipts (
    id           TEXT PRIMARY KEY,
    message_id   TEXT NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
    worker_id    TEXT NOT NULL,
    issued_at    TIMESTAMPTZ NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    extended_at  TIMESTAMPTZ,
    consumed     BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_receipts_message ON receipts (message_id, issued_at);

CREATE TABLE IF NOT EXISTS attempt_failures (
    id           TEXT PRIMARY KEY,
    message_id   TEXT NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
    attempt      BIGINT NOT NULL,
    receipt_id   TEXT NOT NULL,
    worker_id    TEXT NOT NULL,
    cause        TEXT NOT NULL,
    reason       TEXT NOT NULL,
    happened_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_failures_message ON attempt_failures (message_id, happened_at);

CREATE TABLE IF NOT EXISTS events (
    seq            BIGSERIAL PRIMARY KEY,
    event_type     TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    partition_key  TEXT NOT NULL,
    receipt_id     TEXT NOT NULL DEFAULT '',
    worker_id      TEXT NOT NULL DEFAULT '',
    occurred_at    TIMESTAMPTZ NOT NULL,
    attempt        BIGINT NOT NULL DEFAULT 0,
    class          TEXT NOT NULL DEFAULT '',
    reason         TEXT NOT NULL DEFAULT '',
    version        TEXT NOT NULL,
    data           JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_events_message ON events (message_id, seq);
