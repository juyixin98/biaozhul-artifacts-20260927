-- PostgreSQL schema for the local work-message broker.
-- Version: broker-kernel-1.0.0
--
-- Concurrency model:
--   * Claim selects one available row FOR UPDATE SKIP LOCKED, so concurrent
--     workers never take the same message and never block on each other.
--   * Extend/Ack/Nack lock the row referenced by the receipt and perform the
--     kernel's receipt+deadline check and the write in ONE transaction, which
--     is what makes visibility-extension vs timeout atomic.
--   * Dead-lettered rows stay in messages with state='dead' and are excluded
--     from every claim query.

CREATE TABLE IF NOT EXISTS broker_queues (
    name               TEXT PRIMARY KEY,
    visibility_timeout BIGINT NOT NULL CHECK (visibility_timeout > 0), -- nanoseconds
    max_attempts       INTEGER NOT NULL CHECK (max_attempts >= 1),
    created_at         TIMESTAMPTZ NOT NULL,
    schema_version     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS broker_messages (
    id           TEXT PRIMARY KEY,
    queue        TEXT NOT NULL REFERENCES broker_queues(name),
    body         BYTEA NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('available','invisible','dead','acked')),
    attempts     INTEGER NOT NULL DEFAULT 0,           -- actual claim count
    receipt      TEXT NOT NULL DEFAULT '',             -- live lease receipt
    receipt_gen  BIGINT NOT NULL DEFAULT 0,            -- bumped on every claim
    last_receipt TEXT NOT NULL DEFAULT '',             -- previous delivery receipt
    deadline     TIMESTAMPTZ NOT NULL DEFAULT to_timestamp(0),
    enqueued_at  TIMESTAMPTZ NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL,
    failures     JSONB NOT NULL DEFAULT '[]'::jsonb    -- complete failure history
);

CREATE INDEX IF NOT EXISTS broker_messages_claim
    ON broker_messages (queue, id)
    WHERE state = 'available';

CREATE INDEX IF NOT EXISTS broker_messages_dead
    ON broker_messages (queue, id)
    WHERE state = 'dead';

CREATE INDEX IF NOT EXISTS broker_messages_queue
    ON broker_messages (queue, id);

CREATE TABLE IF NOT EXISTS broker_events (
    seq            BIGSERIAL PRIMARY KEY,
    queue          TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    type           TEXT NOT NULL,
    version        TEXT NOT NULL,
    run_id         TEXT NOT NULL DEFAULT '',
    occurred_at    TIMESTAMPTZ NOT NULL,
    to_state       TEXT NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 0,
    receipt        TEXT NOT NULL DEFAULT '',
    receipt_gen    BIGINT NOT NULL DEFAULT 0,
    deadline       TIMESTAMPTZ NOT NULL DEFAULT to_timestamp(0),
    visibility_to  BIGINT NOT NULL DEFAULT 0,
    extra          BIGINT NOT NULL DEFAULT 0,
    failure        JSONB
);

CREATE INDEX IF NOT EXISTS broker_events_queue_seq
    ON broker_events (queue, seq);
